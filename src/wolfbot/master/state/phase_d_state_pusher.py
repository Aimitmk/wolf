"""Phase-D state-update pusher — wired into ``GameService.advance``.

After each transition is applied to the DB, this pusher diff's the new
state against the transition's emitted logs and ships incremental
``PrivateStateUpdate`` messages to each affected seat's NPC bot. Every
update kind the NPC bot recognises is covered:

* ``seer_result`` / ``seer_result`` (NIGHT_0 random white) — when a
  ``SEER_RESULT_NIGHT0`` / ``SEER_RESULT`` private log is emitted.
* ``medium_result`` — on every ``MEDIUM_RESULT`` private log.
* ``guard_entry`` — when a ``KNIGHT_GUARD`` night-action lands.
* ``guard_resolved`` — when MORNING resolves (peaceful or attack).
* ``alive_changed`` / ``day_advanced`` — broadcast to all live LLM
  seats whenever a player dies or the day counter ticks.

This module is the only place that needs to know which Master events
turn into which NPC state mutations, so the NPC bot stays a pure
consumer of the snapshot + update stream.

Pure orchestration — no LLM calls, all data sourced from the repo,
seats, and the transition's logs. Best-effort end to end: a single
failed send is logged but does not block the others.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence

from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import Game, NightAction, Player, Seat
from wolfbot.domain.models import LogEntry as DomainLogEntry
from wolfbot.master.state.private_state import (
    make_alive_changed_update,
    make_day_advanced_update,
    make_guard_entry_update,
    make_guard_resolved_update,
    make_medium_result_update,
    make_seer_result_update,
)
from wolfbot.master.ws.npc_registry import NpcEntry, NpcRegistry
from wolfbot.persistence.sqlite_repo import SqliteRepo

log = logging.getLogger(__name__)


# Regex to pull a seat number out of the canonical ``席N 名前 は ...``
# rendering used by every state-machine private log. The state_machine
# always builds these via ``_name(seats_by_no, ...)`` which produces
# ``席N 名前`` deterministically.
_SEAT_RE = re.compile(r"席(\d+)")


class PhaseDStatePusher:
    """Side-effect coordinator: transitions → PrivateStateUpdate fan-out.

    Hold a reference to the registry + repo and a wall-clock helper.
    The single public entry point is :meth:`push_after_advance`, called
    from ``GameService`` after each successful ``apply_transition``.
    """

    def __init__(
        self,
        *,
        repo: SqliteRepo,
        registry: NpcRegistry,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.repo = repo
        self.registry = registry
        self._now_ms = now_ms

    async def push_after_advance(
        self,
        *,
        game: Game,
        prev_phase: Phase,
        private_logs: Sequence[DomainLogEntry],
        public_logs: Sequence[DomainLogEntry],
    ) -> None:
        """Run all phase-D side-effects for one applied transition.

        ``game`` is the *new* game state (after ``apply_transition``).
        ``private_logs`` / ``public_logs`` are the entries emitted by
        the transition planner; the pusher uses them as triggers and
        re-loads any structured data it needs from the repo.
        """
        if game.discussion_mode != "reactive_voice":
            return
        try:
            seats = await self.repo.load_seats(game.id)
            players = await self.repo.load_players(game.id)
        except Exception:
            log.exception(
                "phase_d_state_pusher_load_failed game=%s", game.id,
            )
            return

        seats_by_no = {s.seat_no: s for s in seats}

        # 1) seer / medium private results — derive structured data and
        # push the matching updates.
        for entry in private_logs:
            if entry.audience_seat is None:
                continue
            if entry.kind in ("SEER_RESULT", "SEER_RESULT_NIGHT0"):
                await self._push_seer_result(
                    game=game,
                    entry=entry,
                    seats_by_no=seats_by_no,
                    players=players,
                )
            elif entry.kind == "MEDIUM_RESULT":
                await self._push_medium_result(
                    game=game,
                    entry=entry,
                    seats_by_no=seats_by_no,
                    players=players,
                )

        # 2) Knight guard ENTRY — pushed when night actions land. Master's
        # state_machine doesn't emit a private log for the knight's own
        # guard choice, so we look directly at the night_actions row.
        await self._push_guard_entries(
            game=game, seats_by_no=seats_by_no, players=players,
        )

        # 3) Knight guard RESOLVED — pushed when MORNING fires.
        if any(e.kind == "MORNING" for e in public_logs):
            await self._push_guard_resolved(
                game=game,
                public_logs=public_logs,
                seats_by_no=seats_by_no,
                players=players,
            )

        # 4) Broad updates: alive_changed (whenever someone dies) +
        # day_advanced (whenever day counter ticks). We always fan these
        # out because the cost is small and the NPC's prompt depends on
        # them.
        await self._push_alive_and_day(
            game=game, prev_phase=prev_phase, players=players, seats=seats,
        )

    # --------------------------------------------------- seer

    async def _push_seer_result(
        self,
        *,
        game: Game,
        entry: DomainLogEntry,
        seats_by_no: dict[int, Seat],
        players: Sequence[Player],
    ) -> None:
        target_seat = _parse_seat_from_text(entry.text)
        if target_seat is None or target_seat not in seats_by_no:
            log.info(
                "phase_d_seer_no_target_in_text game=%s text=%r",
                game.id, entry.text[:80],
            )
            return
        target = next((p for p in players if p.seat_no == target_seat), None)
        if target is None or target.role is None:
            return
        # NIGHT_0 random white is always non-wolf by definition; the
        # regular SEER_RESULT respects the role detection rule (madman is
        # NOT detected as wolf).
        from wolfbot.domain.rules import is_detected_as_wolf

        is_wolf = (
            False
            if entry.kind == "SEER_RESULT_NIGHT0"
            else bool(is_detected_as_wolf(target.role))
        )
        update = make_seer_result_update(
            npc_id="",  # filled per-recipient below
            game_id=game.id,
            seat_no=entry.audience_seat or 0,
            day=entry.day,
            target_seat=target_seat,
            target_name=seats_by_no[target_seat].display_name,
            is_wolf=is_wolf,
            ts=self._now_ms(),
            trace_id=f"seer_result-{game.id}-d{entry.day}",
        )
        await self._send_to_seat(game.id, entry.audience_seat or 0, update)

    # --------------------------------------------------- medium

    async def _push_medium_result(
        self,
        *,
        game: Game,
        entry: DomainLogEntry,
        seats_by_no: dict[int, Seat],
        players: Sequence[Player],
    ) -> None:
        target_seat = _parse_seat_from_text(entry.text)
        is_wolf: bool | None = None
        target_name = "(処刑なし)"
        if target_seat is not None and target_seat in seats_by_no:
            target = next((p for p in players if p.seat_no == target_seat), None)
            target_name = seats_by_no[target_seat].display_name
            if target is not None and target.role is not None:
                from wolfbot.domain.rules import is_detected_as_wolf

                is_wolf = bool(is_detected_as_wolf(target.role))
        else:
            # No execution → medium gets a "no result" update so its
            # state mirror still has a row for that day. target_seat=0
            # is a sentinel meaning "no target"; the NPC prompt-builder
            # already special-cases is_wolf=None.
            target_seat = 0
        update = make_medium_result_update(
            npc_id="",
            game_id=game.id,
            seat_no=entry.audience_seat or 0,
            day=entry.day,
            target_seat=target_seat,
            target_name=target_name,
            is_wolf=is_wolf,
            ts=self._now_ms(),
            trace_id=f"medium_result-{game.id}-d{entry.day}",
        )
        await self._send_to_seat(game.id, entry.audience_seat or 0, update)

    # --------------------------------------------------- knight guard

    async def _push_guard_entries(
        self,
        *,
        game: Game,
        seats_by_no: dict[int, Seat],
        players: Sequence[Player],
    ) -> None:
        knight = next(
            (p for p in players if p.role is Role.KNIGHT and p.alive), None,
        )
        if knight is None:
            return
        try:
            night_actions: list[NightAction] = list(
                await self.repo.load_night_actions(game.id, day=game.day_number)
            )
        except Exception:
            log.exception("phase_d_load_night_actions_failed game=%s", game.id)
            return
        guard_action = next(
            (a for a in night_actions if a.kind is SubmissionType.KNIGHT_GUARD),
            None,
        )
        if guard_action is None or guard_action.target_seat is None:
            return
        target_seat = guard_action.target_seat
        if target_seat not in seats_by_no:
            return
        update = make_guard_entry_update(
            npc_id="",
            game_id=game.id,
            seat_no=knight.seat_no,
            day=game.day_number,
            target_seat=target_seat,
            target_name=seats_by_no[target_seat].display_name,
            ts=self._now_ms(),
            trace_id=f"guard_entry-{game.id}-d{game.day_number}",
        )
        await self._send_to_seat(game.id, knight.seat_no, update)

    async def _push_guard_resolved(
        self,
        *,
        game: Game,
        public_logs: Sequence[DomainLogEntry],
        seats_by_no: dict[int, Seat],
        players: Sequence[Player],
    ) -> None:
        knight = next(
            (p for p in players if p.role is Role.KNIGHT and p.alive), None,
        )
        if knight is None:
            return
        morning = next((e for e in public_logs if e.kind == "MORNING"), None)
        if morning is None:
            return
        peaceful = "平和な朝" in morning.text
        # The morning entry's day is the new day; the guard we're
        # resolving was submitted under the previous day_number.
        update = make_guard_resolved_update(
            npc_id="",
            game_id=game.id,
            seat_no=knight.seat_no,
            day=max(0, game.day_number - 1),
            peaceful_morning=peaceful,
            ts=self._now_ms(),
            trace_id=f"guard_resolved-{game.id}-d{game.day_number}",
        )
        await self._send_to_seat(game.id, knight.seat_no, update)

    # --------------------------------------------------- alive / day

    async def _push_alive_and_day(
        self,
        *,
        game: Game,
        prev_phase: Phase,
        players: Sequence[Player],
        seats: Sequence[Seat],
    ) -> None:
        # Fan out to every alive LLM seat that has an NPC bot online.
        for player in players:
            if not player.alive:
                continue
            seat = next((s for s in seats if s.seat_no == player.seat_no), None)
            if seat is None or not seat.is_llm:
                continue
            entry = self._find_npc_for_seat(game.id, player.seat_no)
            if entry is None or entry.send is None:
                continue
            alive_update = make_alive_changed_update(
                npc_id=entry.npc_id,
                game_id=game.id,
                seat_no=player.seat_no,
                players=players,
                seats=seats,
                ts=self._now_ms(),
                trace_id=f"alive-{game.id}-d{game.day_number}-s{player.seat_no}",
            )
            try:
                await entry.send(alive_update.model_dump_json())
            except Exception:
                log.exception(
                    "phase_d_alive_update_failed npc=%s seat=%d",
                    entry.npc_id, player.seat_no,
                )
            day_update = make_day_advanced_update(
                npc_id=entry.npc_id,
                game_id=game.id,
                seat_no=player.seat_no,
                day_number=game.day_number,
                ts=self._now_ms(),
                trace_id=f"day-{game.id}-d{game.day_number}-s{player.seat_no}",
            )
            try:
                await entry.send(day_update.model_dump_json())
            except Exception:
                log.exception(
                    "phase_d_day_update_failed npc=%s seat=%d",
                    entry.npc_id, player.seat_no,
                )
        # `prev_phase` is currently unused but kept on the signature so
        # future hooks can branch on phase transitions (e.g. emit
        # `day_advanced` only on NIGHT → DAY transitions).
        _ = prev_phase

    # --------------------------------------------------- send helpers

    async def _send_to_seat(
        self,
        game_id: str,
        seat_no: int,
        update_template: object,
    ) -> None:
        entry = self._find_npc_for_seat(game_id, seat_no)
        if entry is None or entry.send is None:
            return
        # The factory returns a PrivateStateUpdate with npc_id="" (we
        # don't know it yet at construction time); patch the field via
        # model_copy so the routed message addresses this NPC.
        try:
            model_copy = getattr(update_template, "model_copy", None)
            if model_copy is None:
                return
            patched = model_copy(update={"npc_id": entry.npc_id})
            payload = patched.model_dump_json()
            await entry.send(payload)
        except Exception:
            log.exception(
                "phase_d_state_update_send_failed game=%s seat=%d",
                game_id, seat_no,
            )

    def _find_npc_for_seat(self, game_id: str, seat_no: int) -> NpcEntry | None:
        for entry in self.registry.all_online():
            if entry.assigned_seat == seat_no and entry.game_id == game_id:
                return entry
        return None


def _parse_seat_from_text(text: str) -> int | None:
    """Pull the first ``席N`` token out of a Japanese private-log text."""
    m = _SEAT_RE.search(text)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


__all__ = ["PhaseDStatePusher"]
