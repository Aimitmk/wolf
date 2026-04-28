"""Master-side `PrivateStateSnapshot` / `PrivateStateUpdate` factories.

Pure-data tests — no DB or asyncio. Verifies that the WS payloads sent to
NPC bots over Phase-D include the right per-seat shape:

* alive/dead seat lists are correctly partitioned + sorted.
* `partner_wolves` only fills for `WEREWOLF` and excludes the recipient.
* Update factories produce well-typed `payload` dicts that the NPC
  bot's `apply_update` can fold (round-trip via `state_from_snapshot` +
  `apply_update` reproduces the expected state).
"""

from __future__ import annotations

from wolfbot.domain.enums import Role
from wolfbot.domain.models import Player, Seat
from wolfbot.master.private_state import (
    build_snapshot_for_seat,
    make_alive_changed_update,
    make_day_advanced_update,
    make_guard_entry_update,
    make_guard_resolved_update,
    make_medium_result_update,
    make_seer_result_update,
    make_wolf_chat_update,
)
from wolfbot.npc.game_state import apply_update, state_from_snapshot


def _seats() -> list[Seat]:
    return [
        Seat(seat_no=1, display_name="Alice", is_llm=False, persona_key=None,
             discord_user_id="u1"),
        Seat(seat_no=2, display_name="Bob", is_llm=True, persona_key="setsu",
             discord_user_id=None),
        Seat(seat_no=3, display_name="Carol", is_llm=True, persona_key="gina",
             discord_user_id=None),
        Seat(seat_no=4, display_name="Dave", is_llm=True, persona_key="jonas",
             discord_user_id=None),
    ]


def _players_with_one_wolf_dead() -> list[Player]:
    """Two wolves total, one alive (seat 2), one dead (seat 3)."""
    return [
        Player(seat_no=1, role=Role.VILLAGER, alive=True),
        Player(seat_no=2, role=Role.WEREWOLF, alive=True),
        Player(seat_no=3, role=Role.WEREWOLF, alive=False,
               death_cause=None, death_day=1),
        Player(seat_no=4, role=Role.SEER, alive=True),
    ]


def test_snapshot_partitions_alive_dead_and_sorts() -> None:
    snap = build_snapshot_for_seat(
        npc_id="npc_setsu",
        game_id="g1",
        seat_no=2,
        persona_key="setsu",
        role=Role.WEREWOLF,
        day_number=2,
        players=_players_with_one_wolf_dead(),
        seats=_seats(),
        ts=1000,
        trace_id="t",
    )
    assert snap.alive_seats == ((1, "Alice"), (2, "Bob"), (4, "Dave"))
    assert snap.dead_seats == ((3, "Carol"),)
    assert snap.role == "WEREWOLF"
    assert snap.day_number == 2


def test_snapshot_partner_wolves_only_for_werewolf_excludes_self_and_dead() -> None:
    # Recipient is the alive wolf (seat 2) — partner is seat 3 but dead.
    snap = build_snapshot_for_seat(
        npc_id="npc_setsu",
        game_id="g1",
        seat_no=2,
        persona_key="setsu",
        role=Role.WEREWOLF,
        day_number=2,
        players=_players_with_one_wolf_dead(),
        seats=_seats(),
        ts=1000,
        trace_id="t",
    )
    # Dead partner is excluded; recipient is excluded; nothing left.
    assert snap.partner_wolves == ()


def test_snapshot_partner_wolves_excludes_self() -> None:
    players = [
        Player(seat_no=1, role=Role.VILLAGER, alive=True),
        Player(seat_no=2, role=Role.WEREWOLF, alive=True),
        Player(seat_no=3, role=Role.WEREWOLF, alive=True),
    ]
    seats = [
        Seat(seat_no=n, display_name=name, is_llm=True, persona_key="x",
             discord_user_id=None)
        for n, name in [(1, "Alice"), (2, "Bob"), (3, "Carol")]
    ]
    snap = build_snapshot_for_seat(
        npc_id="npc_x",
        game_id="g1",
        seat_no=2,
        persona_key="setsu",
        role=Role.WEREWOLF,
        day_number=1,
        players=players,
        seats=seats,
        ts=1000,
        trace_id="t",
    )
    # Only seat 3 (the other living wolf) is in partner_wolves.
    assert snap.partner_wolves == ((3, "Carol"),)


def test_snapshot_non_wolf_role_has_no_partners() -> None:
    snap = build_snapshot_for_seat(
        npc_id="npc_jonas",
        game_id="g1",
        seat_no=4,
        persona_key="jonas",
        role=Role.SEER,
        day_number=2,
        players=_players_with_one_wolf_dead(),
        seats=_seats(),
        ts=1000,
        trace_id="t",
    )
    assert snap.partner_wolves == ()


def test_seer_result_update_round_trips_into_state() -> None:
    snap = build_snapshot_for_seat(
        npc_id="npc_jonas", game_id="g1", seat_no=4, persona_key="jonas",
        role=Role.SEER, day_number=1,
        players=_players_with_one_wolf_dead(), seats=_seats(),
        ts=1000, trace_id="t",
    )
    state = state_from_snapshot(snap)
    upd = make_seer_result_update(
        npc_id="npc_jonas", game_id="g1", seat_no=4,
        day=1, target_seat=2, target_name="Bob", is_wolf=True,
        ts=2000, trace_id="t2",
    )
    apply_update(state, upd)
    assert len(state.seer_results) == 1
    sr = state.seer_results[0]
    assert sr.target_seat == 2 and sr.is_wolf is True


def test_medium_result_update_can_carry_null_is_wolf() -> None:
    snap = build_snapshot_for_seat(
        npc_id="npc_x", game_id="g1", seat_no=1, persona_key="x",
        role=Role.MEDIUM, day_number=1,
        players=[Player(seat_no=1, role=Role.MEDIUM, alive=True)],
        seats=[Seat(seat_no=1, display_name="Alice", is_llm=True, persona_key="x", discord_user_id=None)],
        ts=1, trace_id="t",
    )
    state = state_from_snapshot(snap)
    upd = make_medium_result_update(
        npc_id="npc_x", game_id="g1", seat_no=1,
        day=1, target_seat=2, target_name="Bob", is_wolf=None,
        ts=2, trace_id="t2",
    )
    apply_update(state, upd)
    assert state.medium_results[0].is_wolf is None


def test_guard_entry_then_resolved_fills_peaceful_flag() -> None:
    snap = build_snapshot_for_seat(
        npc_id="npc_x", game_id="g1", seat_no=1, persona_key="x",
        role=Role.KNIGHT, day_number=1,
        players=[Player(seat_no=1, role=Role.KNIGHT, alive=True)],
        seats=[Seat(seat_no=1, display_name="Alice", is_llm=True, persona_key="x", discord_user_id=None)],
        ts=1, trace_id="t",
    )
    state = state_from_snapshot(snap)
    apply_update(state, make_guard_entry_update(
        npc_id="npc_x", game_id="g1", seat_no=1,
        day=1, target_seat=2, target_name="Bob", ts=2, trace_id="t2",
    ))
    apply_update(state, make_guard_resolved_update(
        npc_id="npc_x", game_id="g1", seat_no=1,
        day=1, peaceful_morning=True, ts=3, trace_id="t3",
    ))
    assert state.guard_history[0].peaceful_morning is True


def test_wolf_chat_update_appends() -> None:
    snap = build_snapshot_for_seat(
        npc_id="npc_x", game_id="g1", seat_no=2, persona_key="x",
        role=Role.WEREWOLF, day_number=1,
        players=_players_with_one_wolf_dead(), seats=_seats(),
        ts=1, trace_id="t",
    )
    state = state_from_snapshot(snap)
    apply_update(state, make_wolf_chat_update(
        npc_id="npc_x", game_id="g1", seat_no=2,
        day=1, speaker_seat=3, speaker_name="Carol",
        text="席1を狙おう", ts=2, trace_id="t2",
    ))
    assert state.wolf_chat_history[-1].text == "席1を狙おう"


def test_alive_changed_update_replaces_lists() -> None:
    snap = build_snapshot_for_seat(
        npc_id="npc_x", game_id="g1", seat_no=1, persona_key="x",
        role=Role.VILLAGER, day_number=1,
        players=_players_with_one_wolf_dead(), seats=_seats(),
        ts=1, trace_id="t",
    )
    state = state_from_snapshot(snap)
    new_players = list(_players_with_one_wolf_dead())
    new_players[0] = Player(seat_no=1, role=Role.VILLAGER, alive=False,
                            death_cause=None, death_day=2)
    apply_update(state, make_alive_changed_update(
        npc_id="npc_x", game_id="g1", seat_no=1,
        players=new_players, seats=_seats(), ts=2, trace_id="t2",
    ))
    alive_set = {p[0] for p in state.alive_seats}
    dead_set = {p[0] for p in state.dead_seats}
    assert 1 in dead_set and 1 not in alive_set


def test_day_advanced_update_increments_day() -> None:
    snap = build_snapshot_for_seat(
        npc_id="npc_x", game_id="g1", seat_no=1, persona_key="x",
        role=Role.VILLAGER, day_number=1,
        players=_players_with_one_wolf_dead(), seats=_seats(),
        ts=1, trace_id="t",
    )
    state = state_from_snapshot(snap)
    apply_update(state, make_day_advanced_update(
        npc_id="npc_x", game_id="g1", seat_no=1,
        day_number=3, ts=2, trace_id="t2",
    ))
    assert state.day_number == 3
