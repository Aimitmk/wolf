"""Master-side builder for `PrivateStateSnapshot` / `PrivateStateUpdate`.

In Phase-D (`reactive_voice` mode), each NPC bot is the embodied agent
for its seat and decides speech / vote / night-action via its own LLM.
The bot needs role + role-specific result history + wolf chat to make
those decisions; Master is the source of truth for all of it.

This module turns Master DB rows into the WS payloads:

* :func:`build_snapshot_for_seat` — full state replace, sent at game
  start and on NPC re-register. Pure function of supplied data.
* :func:`load_private_state_for_seat` — async helper that reads
  ``logs_private`` + ``night_actions`` and rebuilds the per-role
  histories the snapshot carries (seer / medium / guard / wolf-chat).
  Used by ``main.py`` so a snapshot push after a Master restart or
  a delayed first-phase entry recovers the seer's day-0 random white,
  the medium's past results, the knight's guard history, and the
  wolves' chat log.
* update factories (e.g. :func:`make_seer_result_update`) — per-event
  patches sent as Master computes new private results.

The *snapshot builder* stays a pure function; the *loader* does I/O
against the repo. Callers in main.py / arbiter / state-machine glue
own the WS send.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from wolfbot.domain.enums import Role, SubmissionType
from wolfbot.domain.models import Player, Seat
from wolfbot.domain.ws_messages import (
    GuardEntry,
    MediumResult,
    PrivateStateSnapshot,
    PrivateStateUpdate,
    SeerResult,
    WolfChatLine,
)

if TYPE_CHECKING:
    from wolfbot.persistence.sqlite_repo import SqliteRepo

log = logging.getLogger(__name__)


def _seat_pairs(
    players: Sequence[Player],
    seats_by_no: dict[int, Seat],
    *,
    alive: bool,
) -> tuple[tuple[int, str], ...]:
    """``(seat_no, display_name)`` pairs filtered by alive state, sorted by seat."""
    return tuple(
        sorted(
            (p.seat_no, seats_by_no[p.seat_no].display_name)
            for p in players
            if p.alive is alive and p.seat_no in seats_by_no
        )
    )


def _partner_wolves(
    players: Sequence[Player],
    seats_by_no: dict[int, Seat],
    *,
    self_seat: int,
) -> tuple[tuple[int, str], ...]:
    """Wolves other than ``self_seat``, alive only, sorted by seat.

    Empty for non-wolves; the caller must only invoke this after
    confirming the recipient is `Role.WEREWOLF`.
    """
    return tuple(
        sorted(
            (p.seat_no, seats_by_no[p.seat_no].display_name)
            for p in players
            if p.role is Role.WEREWOLF
            and p.seat_no != self_seat
            and p.alive
            and p.seat_no in seats_by_no
        )
    )


def build_snapshot_for_seat(
    *,
    npc_id: str,
    game_id: str,
    seat_no: int,
    persona_key: str,
    role: Role,
    day_number: int,
    players: Sequence[Player],
    seats: Sequence[Seat],
    seer_results: Sequence[SeerResult] = (),
    medium_results: Sequence[MediumResult] = (),
    guard_history: Sequence[GuardEntry] = (),
    wolf_chat_history: Sequence[WolfChatLine] = (),
    ts: int,
    trace_id: str,
) -> PrivateStateSnapshot:
    """Compose the full snapshot the NPC bot rebuilds its state from."""
    seats_by_no = {s.seat_no: s for s in seats}
    # Death-cause tag per dead seat so the NPC prompt can distinguish
    # yesterday's executions from last night's attacks. Players with no
    # cause (= still alive, or some imported state) are skipped.
    dead_causes = tuple(
        sorted(
            (p.seat_no, p.death_cause.value)
            for p in players
            if not p.alive and p.death_cause is not None
        )
    )
    return PrivateStateSnapshot(
        ts=ts,
        trace_id=trace_id,
        npc_id=npc_id,
        game_id=game_id,
        seat_no=seat_no,
        persona_key=persona_key,
        role=role.value,
        day_number=day_number,
        alive_seats=_seat_pairs(players, seats_by_no, alive=True),
        dead_seats=_seat_pairs(players, seats_by_no, alive=False),
        dead_seat_causes=dead_causes,
        partner_wolves=(
            _partner_wolves(players, seats_by_no, self_seat=seat_no)
            if role is Role.WEREWOLF
            else ()
        ),
        seer_results=tuple(seer_results),
        medium_results=tuple(medium_results),
        guard_history=tuple(guard_history),
        wolf_chat_history=tuple(wolf_chat_history),
    )


# --- DB → snapshot history loader ------------------------------------

# Private logs persist results as natural Japanese text; the structured
# fields (target_seat, is_wolf) are not stored in dedicated columns.
# Parse them back out here so the NPC's snapshot can carry the same
# info rounds-mode prompt builds get from raw text reads.
_SEER_NIGHT0_RE = re.compile(r"^初日ランダム白:\s*(?P<name>.+?)\s+は")
_SEER_RESULT_RE = re.compile(r"^占い結果:\s*(?P<name>.+?)\s+は")
_MEDIUM_RESULT_RE = re.compile(r"^霊媒結果:\s*(?P<name>.+?)\s+は")


def _resolve_seat_by_name(
    name: str, seats_by_no: dict[int, Seat]
) -> tuple[int, str] | None:
    """Match a parsed display name back to a seat. Tries exact match
    first, then strips a leading emoji+space prefix as a fall-back so
    the seer's NIGHT_0 random-white text "👑ユリコ" still resolves
    when display_name lookup uses a different normalization."""
    for seat in seats_by_no.values():
        if seat.display_name == name:
            return (seat.seat_no, seat.display_name)
    # Fall-back: try stripping leading non-word characters (emoji etc.)
    stripped = name.lstrip()
    while stripped and not (stripped[0].isalnum() or "ぁ" <= stripped[0] <= "ヿ"):
        stripped = stripped[1:]
    if stripped:
        for seat in seats_by_no.values():
            if seat.display_name.endswith(stripped):
                return (seat.seat_no, seat.display_name)
    return None


async def load_private_state_for_seat(
    repo: SqliteRepo,
    *,
    game_id: str,
    seat_no: int,
    role: Role,
    players: Sequence[Player],
    seats: Sequence[Seat],
) -> tuple[
    tuple[SeerResult, ...],
    tuple[MediumResult, ...],
    tuple[GuardEntry, ...],
    tuple[WolfChatLine, ...],
]:
    """Rebuild role-specific history for a seat from persisted DB rows.

    Returns ``(seer_results, medium_results, guard_history,
    wolf_chat_history)``. Each tuple is non-empty only when the role
    actually owns that history:

    * ``seer_results`` — populated for ``Role.SEER`` from
      SEER_RESULT_NIGHT0 + SEER_RESULT private logs.
    * ``medium_results`` — populated for ``Role.MEDIUM`` from
      MEDIUM_RESULT private logs.
    * ``guard_history`` — populated for ``Role.KNIGHT`` from
      ``night_actions`` (KNIGHT_GUARD entries) joined with the
      MORNING public log to derive ``peaceful_morning``.
    * ``wolf_chat_history`` — populated for ``Role.WEREWOLF`` from
      WOLF_CHAT private logs (every wolf seat sees them via the
      ``audience_seat IS NULL`` branch of ``load_private_logs``).

    Best-effort: a parse failure on one row is logged and skipped, not
    fatal — a partially populated snapshot is still strictly better
    than the previous always-empty default.
    """
    seats_by_no = {s.seat_no: s for s in seats}

    seer_results: list[SeerResult] = []
    medium_results: list[MediumResult] = []
    wolf_chat_history: list[WolfChatLine] = []
    guard_history: list[GuardEntry] = []

    if role in (Role.SEER, Role.MEDIUM):
        try:
            rows = await repo.load_private_logs_for_audience(
                game_id, audience_seat=seat_no, limit=200,
            )
        except Exception:
            log.exception(
                "private_state_load_failed_seer_medium game=%s seat=%d",
                game_id, seat_no,
            )
            rows = []
        for r in rows:
            day = int(r["day"])
            text = str(r["text"])
            kind = r["kind"]
            if role is Role.SEER and kind == "SEER_RESULT_NIGHT0":
                m = _SEER_NIGHT0_RE.match(text)
                if m is None:
                    continue
                resolved = _resolve_seat_by_name(m.group("name"), seats_by_no)
                if resolved is None:
                    continue
                target_seat, target_name = resolved
                seer_results.append(
                    SeerResult(
                        day=day, target_seat=target_seat,
                        target_name=target_name, is_wolf=False,
                    )
                )
            elif role is Role.SEER and kind == "SEER_RESULT":
                m = _SEER_RESULT_RE.match(text)
                if m is None:
                    continue
                resolved = _resolve_seat_by_name(m.group("name"), seats_by_no)
                if resolved is None:
                    continue
                target_seat, target_name = resolved
                is_wolf = "ありません" not in text
                seer_results.append(
                    SeerResult(
                        day=day, target_seat=target_seat,
                        target_name=target_name, is_wolf=is_wolf,
                    )
                )
            elif role is Role.MEDIUM and kind == "MEDIUM_RESULT":
                m = _MEDIUM_RESULT_RE.match(text)
                if m is None:
                    # "本日の霊媒結果はありません(処刑なし)。" — just skip.
                    continue
                resolved = _resolve_seat_by_name(m.group("name"), seats_by_no)
                if resolved is None:
                    continue
                target_seat, target_name = resolved
                is_wolf = "ありませんでした" not in text
                medium_results.append(
                    MediumResult(
                        day=day, target_seat=target_seat,
                        target_name=target_name, is_wolf=is_wolf,
                    )
                )

    if role is Role.WEREWOLF:
        try:
            rows = await repo.load_private_logs_for_audience(
                game_id, audience_seat=seat_no, limit=200,
            )
        except Exception:
            log.exception(
                "private_state_load_failed_wolf_chat game=%s seat=%d",
                game_id, seat_no,
            )
            rows = []
        for r in rows:
            if r["kind"] != "WOLF_CHAT":
                continue
            speaker_seat = r["actor_seat"]
            if speaker_seat is None or speaker_seat not in seats_by_no:
                continue
            speaker_name = seats_by_no[speaker_seat].display_name
            wolf_chat_history.append(
                WolfChatLine(
                    day=int(r["day"]),
                    speaker_seat=int(speaker_seat),
                    speaker_name=speaker_name,
                    text=str(r["text"]),
                )
            )

    if role is Role.KNIGHT:
        # Knight's guard targets come from the night_actions table for
        # every day the knight has been alive. Each guard entry's
        # ``peaceful_morning`` is derived from the next day's MORNING
        # public log.
        for p in players:
            if p.seat_no == seat_no and p.role is Role.KNIGHT:
                break
        else:
            return (
                tuple(seer_results), tuple(medium_results),
                tuple(guard_history), tuple(wolf_chat_history),
            )
        try:
            public_logs = await repo.load_public_logs(game_id, limit=200)
        except Exception:
            log.exception(
                "private_state_load_failed_public_logs game=%s", game_id,
            )
            public_logs = []
        morning_by_day: dict[int, bool] = {}
        for log_row in public_logs:
            if log_row.get("kind") == "MORNING":
                # MORNING is emitted for the *new* day after a NIGHT
                # resolution; the guard it resolves was submitted on
                # ``day - 1``. Stash by submission day.
                resolved_day = int(log_row.get("day", 0)) - 1
                if resolved_day < 0:
                    continue
                peaceful = "平和な朝" in str(log_row.get("text", ""))
                morning_by_day[resolved_day] = peaceful
        try:
            seen_days: set[int] = set()
            # 9-player game runs at most a handful of nights; loading per-day is fine.
            for day in range(0, 30):
                actions = await repo.load_night_actions(game_id, day=day)
                for a in actions:
                    if (
                        a.actor_seat == seat_no
                        and a.kind is SubmissionType.KNIGHT_GUARD
                        and a.target_seat is not None
                        and a.target_seat in seats_by_no
                        and day not in seen_days
                    ):
                        seen_days.add(day)
                        guard_history.append(
                            GuardEntry(
                                day=day,
                                target_seat=a.target_seat,
                                target_name=seats_by_no[a.target_seat].display_name,
                                peaceful_morning=morning_by_day.get(day),
                            )
                        )
                if not actions and day > 1:
                    # No actions ⇒ later days won't have any either.
                    break
        except Exception:
            log.exception(
                "private_state_load_failed_guard game=%s seat=%d",
                game_id, seat_no,
            )

    return (
        tuple(seer_results),
        tuple(medium_results),
        tuple(guard_history),
        tuple(wolf_chat_history),
    )


def make_seer_result_update(
    *,
    npc_id: str,
    game_id: str,
    seat_no: int,
    day: int,
    target_seat: int,
    target_name: str,
    is_wolf: bool,
    ts: int,
    trace_id: str,
) -> PrivateStateUpdate:
    return PrivateStateUpdate(
        ts=ts,
        trace_id=trace_id,
        npc_id=npc_id,
        game_id=game_id,
        seat_no=seat_no,
        update_kind="seer_result",
        payload={
            "day": day,
            "target_seat": target_seat,
            "target_name": target_name,
            "is_wolf": is_wolf,
        },
    )


def make_medium_result_update(
    *,
    npc_id: str,
    game_id: str,
    seat_no: int,
    day: int,
    target_seat: int,
    target_name: str,
    is_wolf: bool | None,
    ts: int,
    trace_id: str,
) -> PrivateStateUpdate:
    return PrivateStateUpdate(
        ts=ts,
        trace_id=trace_id,
        npc_id=npc_id,
        game_id=game_id,
        seat_no=seat_no,
        update_kind="medium_result",
        payload={
            "day": day,
            "target_seat": target_seat,
            "target_name": target_name,
            "is_wolf": is_wolf,
        },
    )


def make_guard_entry_update(
    *,
    npc_id: str,
    game_id: str,
    seat_no: int,
    day: int,
    target_seat: int,
    target_name: str,
    ts: int,
    trace_id: str,
) -> PrivateStateUpdate:
    return PrivateStateUpdate(
        ts=ts,
        trace_id=trace_id,
        npc_id=npc_id,
        game_id=game_id,
        seat_no=seat_no,
        update_kind="guard_entry",
        payload={
            "day": day,
            "target_seat": target_seat,
            "target_name": target_name,
        },
    )


def make_guard_resolved_update(
    *,
    npc_id: str,
    game_id: str,
    seat_no: int,
    day: int,
    peaceful_morning: bool,
    ts: int,
    trace_id: str,
) -> PrivateStateUpdate:
    return PrivateStateUpdate(
        ts=ts,
        trace_id=trace_id,
        npc_id=npc_id,
        game_id=game_id,
        seat_no=seat_no,
        update_kind="guard_resolved",
        payload={
            "day": day,
            "peaceful_morning": peaceful_morning,
        },
    )


def make_wolf_chat_update(
    *,
    npc_id: str,
    game_id: str,
    seat_no: int,
    day: int,
    speaker_seat: int,
    speaker_name: str,
    text: str,
    ts: int,
    trace_id: str,
) -> PrivateStateUpdate:
    return PrivateStateUpdate(
        ts=ts,
        trace_id=trace_id,
        npc_id=npc_id,
        game_id=game_id,
        seat_no=seat_no,
        update_kind="wolf_chat",
        payload={
            "day": day,
            "speaker_seat": speaker_seat,
            "speaker_name": speaker_name,
            "text": text,
        },
    )


def make_alive_changed_update(
    *,
    npc_id: str,
    game_id: str,
    seat_no: int,
    players: Sequence[Player],
    seats: Sequence[Seat],
    ts: int,
    trace_id: str,
) -> PrivateStateUpdate:
    seats_by_no = {s.seat_no: s for s in seats}
    return PrivateStateUpdate(
        ts=ts,
        trace_id=trace_id,
        npc_id=npc_id,
        game_id=game_id,
        seat_no=seat_no,
        update_kind="alive_changed",
        payload={
            "alive_seats": [
                [pair[0], pair[1]]
                for pair in _seat_pairs(players, seats_by_no, alive=True)
            ],
            "dead_seats": [
                [pair[0], pair[1]]
                for pair in _seat_pairs(players, seats_by_no, alive=False)
            ],
            # Death-cause tag per dead seat. NPC prompt uses this to
            # distinguish 「昨日処刑された」 from 「昨夜襲われた」 — without
            # it the model regularly conflates yesterday's vote victim
            # with last night's attack victim.
            "dead_seat_causes": [
                [p.seat_no, p.death_cause.value]
                for p in players
                if not p.alive and p.death_cause is not None
            ],
        },
    )


def make_day_advanced_update(
    *,
    npc_id: str,
    game_id: str,
    seat_no: int,
    day_number: int,
    ts: int,
    trace_id: str,
) -> PrivateStateUpdate:
    return PrivateStateUpdate(
        ts=ts,
        trace_id=trace_id,
        npc_id=npc_id,
        game_id=game_id,
        seat_no=seat_no,
        update_kind="day_advanced",
        payload={"day_number": day_number},
    )


__all__ = [
    "build_snapshot_for_seat",
    "load_private_state_for_seat",
    "make_alive_changed_update",
    "make_day_advanced_update",
    "make_guard_entry_update",
    "make_guard_resolved_update",
    "make_medium_result_update",
    "make_seer_result_update",
    "make_wolf_chat_update",
]
