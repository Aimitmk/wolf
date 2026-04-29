"""Per-game in-memory state held by an NPC bot.

Phase-D architecture (`reactive_voice` mode): each NPC bot is the embodied
agent for its seat. It owns the role + role-specific results + wolf chat
history and uses them when deciding speech / vote / night action via its
own `NPC_LLM_*`. Master pushes state via `PrivateStateSnapshot` (full
replace) and `PrivateStateUpdate` (append/patch) so the NPC never reads
the Master DB or persists state locally — a process restart re-hydrates
from a Master-sent snapshot at re-register.

This module is the pure data container. The dispatch / WS handlers live
in :mod:`wolfbot.npc.client`; the prompt-building consumers live in the
NPC LLM generators.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wolfbot.domain.ws_messages import (
    GuardEntry,
    MediumResult,
    PrivateStateSnapshot,
    PrivateStateUpdate,
    SeerResult,
    WolfChatLine,
)


def _as_int(value: Any) -> int:
    """Coerce a JSON-decoded payload value to int. Raises ``TypeError`` on bool
    (which would otherwise silently be treated as 0/1) and missing values."""
    if isinstance(value, bool) or value is None:
        raise TypeError(f"expected int-like, got {type(value).__name__}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"expected int-like, got {type(value).__name__}")


def _as_str(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    return value


def _as_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"expected bool, got {type(value).__name__}")
    return value


def _opt_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return _as_bool(value)


@dataclass
class NpcGameState:
    """Per-(game, seat) state mirror.

    The NPC bot keeps one of these per active game. Snapshots replace it
    wholesale; updates mutate it in place via ``apply_update``. Mutability
    is a deliberate choice — the WS handler fast path stays cheap and the
    state is process-local.
    """

    game_id: str
    seat_no: int
    persona_key: str
    role: str
    day_number: int = 0
    alive_seats: list[tuple[int, str]] = field(default_factory=list)
    dead_seats: list[tuple[int, str]] = field(default_factory=list)
    # Per-dead-seat death cause label ("EXECUTION" | "ATTACK"), keyed by
    # seat_no. Lets the prompt builder distinguish yesterday's vote
    # victims from last night's attack victims so the NPC stops saying
    # 「昨夜の犠牲者は◯◯」 about a player who was actually executed.
    dead_seat_causes: dict[int, str] = field(default_factory=dict)
    partner_wolves: list[tuple[int, str]] = field(default_factory=list)
    seer_results: list[SeerResult] = field(default_factory=list)
    medium_results: list[MediumResult] = field(default_factory=list)
    guard_history: list[GuardEntry] = field(default_factory=list)
    wolf_chat_history: list[WolfChatLine] = field(default_factory=list)


def state_from_snapshot(snapshot: PrivateStateSnapshot) -> NpcGameState:
    """Rebuild state from a `PrivateStateSnapshot` (full replace semantics)."""
    return NpcGameState(
        game_id=snapshot.game_id,
        seat_no=snapshot.seat_no,
        persona_key=snapshot.persona_key,
        role=snapshot.role,
        day_number=snapshot.day_number,
        alive_seats=list(snapshot.alive_seats),
        dead_seats=list(snapshot.dead_seats),
        dead_seat_causes={
            seat_no: cause for seat_no, cause in snapshot.dead_seat_causes
        },
        partner_wolves=list(snapshot.partner_wolves),
        seer_results=list(snapshot.seer_results),
        medium_results=list(snapshot.medium_results),
        guard_history=list(snapshot.guard_history),
        wolf_chat_history=list(snapshot.wolf_chat_history),
    )


def apply_update(state: NpcGameState, update: PrivateStateUpdate) -> None:
    """Mutate `state` in place by `update`. Unknown kinds are silently
    ignored so a newer Master can add update kinds without breaking older
    NPC builds.

    Each branch tolerates malformed payloads (missing/typed-wrong keys)
    by skipping the update — the in-memory state stays consistent rather
    than crashing the bot.
    """
    kind = update.update_kind
    payload = update.payload
    try:
        if kind == "seer_result":
            state.seer_results.append(
                SeerResult(
                    day=_as_int(payload["day"]),
                    target_seat=_as_int(payload["target_seat"]),
                    target_name=_as_str(payload["target_name"]),
                    is_wolf=_as_bool(payload["is_wolf"]),
                )
            )
        elif kind == "medium_result":
            state.medium_results.append(
                MediumResult(
                    day=_as_int(payload["day"]),
                    target_seat=_as_int(payload["target_seat"]),
                    target_name=_as_str(payload["target_name"]),
                    is_wolf=_opt_bool(payload.get("is_wolf")),
                )
            )
        elif kind == "guard_entry":
            state.guard_history.append(
                GuardEntry(
                    day=_as_int(payload["day"]),
                    target_seat=_as_int(payload["target_seat"]),
                    target_name=_as_str(payload["target_name"]),
                    peaceful_morning=_opt_bool(payload.get("peaceful_morning")),
                )
            )
        elif kind == "guard_resolved":
            day = _as_int(payload["day"])
            peaceful = _as_bool(payload["peaceful_morning"])
            state.guard_history = [
                entry.model_copy(update={"peaceful_morning": peaceful})
                if entry.day == day and entry.peaceful_morning is None
                else entry
                for entry in state.guard_history
            ]
        elif kind == "wolf_chat":
            state.wolf_chat_history.append(
                WolfChatLine(
                    day=_as_int(payload["day"]),
                    speaker_seat=_as_int(payload["speaker_seat"]),
                    speaker_name=_as_str(payload["speaker_name"]),
                    text=_as_str(payload["text"]),
                )
            )
        elif kind == "alive_changed":
            alive = payload.get("alive_seats", [])
            dead = payload.get("dead_seats", [])
            causes = payload.get("dead_seat_causes", [])
            if isinstance(alive, list):
                state.alive_seats = [
                    (_as_int(s[0]), _as_str(s[1]))
                    for s in alive
                    if isinstance(s, (list, tuple)) and len(s) >= 2
                ]
            if isinstance(dead, list):
                state.dead_seats = [
                    (_as_int(s[0]), _as_str(s[1]))
                    for s in dead
                    if isinstance(s, (list, tuple)) and len(s) >= 2
                ]
            if isinstance(causes, list):
                state.dead_seat_causes = {
                    _as_int(s[0]): _as_str(s[1])
                    for s in causes
                    if isinstance(s, (list, tuple)) and len(s) >= 2
                }
        elif kind == "day_advanced":
            state.day_number = _as_int(payload["day_number"])
        # Unknown kinds: silently skip for forward-compat.
    except (KeyError, TypeError, ValueError):
        # Best-effort — malformed payload from Master shouldn't crash the bot.
        pass


__all__ = [
    "NpcGameState",
    "apply_update",
    "state_from_snapshot",
]
