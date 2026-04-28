"""Master-side builder for `PrivateStateSnapshot` / `PrivateStateUpdate`.

In Phase-D (`reactive_voice` mode), each NPC bot is the embodied agent
for its seat and decides speech / vote / night-action via its own LLM.
The bot needs role + role-specific result history + wolf chat to make
those decisions; Master is the source of truth for all of it.

This module turns Master DB rows into the WS payloads:

* :func:`build_snapshot_for_seat` — full state replace, sent at game
  start and on NPC re-register.
* update factories (e.g. :func:`make_seer_result_update`) — per-event
  patches sent as Master computes new private results.

Pure functions of repo data + ws_messages — no I/O, no asyncio. The
callers (main.py / arbiter / state-machine glue) own the WS send.
"""

from __future__ import annotations

from collections.abc import Sequence

from wolfbot.domain.enums import Role
from wolfbot.domain.models import Player, Seat
from wolfbot.domain.ws_messages import (
    GuardEntry,
    MediumResult,
    PrivateStateSnapshot,
    PrivateStateUpdate,
    SeerResult,
    WolfChatLine,
)


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
    "make_alive_changed_update",
    "make_day_advanced_update",
    "make_guard_entry_update",
    "make_guard_resolved_update",
    "make_medium_result_update",
    "make_seer_result_update",
    "make_wolf_chat_update",
]
