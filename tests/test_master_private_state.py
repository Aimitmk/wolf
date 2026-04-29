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

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import Game, LogEntry, NightAction, Player, Seat
from wolfbot.master.private_state import (
    build_snapshot_for_seat,
    load_private_state_for_seat,
    make_alive_changed_update,
    make_day_advanced_update,
    make_guard_entry_update,
    make_guard_resolved_update,
    make_medium_result_update,
    make_seer_result_update,
    make_wolf_chat_update,
)
from wolfbot.npc.game_state import apply_update, state_from_snapshot
from wolfbot.persistence.schema import migrate
from wolfbot.persistence.sqlite_repo import SqliteRepo


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


# ---- DB → snapshot history loader ------------------------------------


@pytest_asyncio.fixture
async def fresh_repo() -> AsyncIterator[SqliteRepo]:
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        await migrate(db)
        r = SqliteRepo(db)
        await r.connect()
        try:
            yield r
        finally:
            await r.close()


async def _seed_game_with_seer(repo: SqliteRepo) -> Game:
    """A minimal game with seat 5 = SEER and seat 9 = the night-0
    random-white target. Only enough rows for the loader to parse."""
    g = Game(
        id="snap_seed",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(g)
    seats = [
        Seat(seat_no=i, display_name=f"NPC{i}", is_llm=True,
             persona_key=None, discord_user_id=None)
        for i in range(1, 10)
    ]
    seats[8] = Seat(  # seat 9 with emoji prefix to mirror live data
        seat_no=9, display_name="👑ユリコ", is_llm=True,
        persona_key="yuriko", discord_user_id=None,
    )
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 5, Role.SEER)
    await repo.set_player_role(g.id, 9, Role.VILLAGER)
    return g


async def test_load_private_state_seer_recovers_night0_random_white(
    fresh_repo: SqliteRepo,
) -> None:
    """The seer's NIGHT_0 random white must round-trip through the
    snapshot loader. Pre-fix the snapshot was always built with
    seer_results=() — Gina's day-1 user prompt had no `## 自分の占い結果`
    block and she correctly chose not to fabricate a CO without data."""
    g = await _seed_game_with_seer(fresh_repo)
    await fresh_repo.insert_log_private(
        LogEntry(
            game_id=g.id, day=0, phase=Phase.NIGHT_0,
            kind="SEER_RESULT_NIGHT0",
            actor_seat=None, audience_seat=5, visibility="PRIVATE",
            text="初日ランダム白: 👑ユリコ は 人狼ではありません。",
            created_at=1,
        )
    )
    seers, mediums, guards, wolves, attacks = await load_private_state_for_seat(
        fresh_repo, game_id=g.id, seat_no=5, role=Role.SEER,
        players=await fresh_repo.load_players(g.id),
        seats=await fresh_repo.load_seats(g.id),
    )
    assert mediums == () and guards == () and wolves == () and attacks == ()
    assert len(seers) == 1
    assert seers[0].day == 0
    assert seers[0].target_seat == 9
    assert seers[0].target_name == "👑ユリコ"
    assert seers[0].is_wolf is False


async def test_load_private_state_seer_parses_day1_black_and_white(
    fresh_repo: SqliteRepo,
) -> None:
    """Day 1+ SEER_RESULT log text format ('〜 は 人狼です' / '〜 は
    人狼ではありません'). Both branches resolve to the right is_wolf."""
    g = await _seed_game_with_seer(fresh_repo)
    await fresh_repo.insert_log_private(
        LogEntry(
            game_id=g.id, day=1, phase=Phase.NIGHT,
            kind="SEER_RESULT", actor_seat=None, audience_seat=5,
            visibility="PRIVATE",
            text="占い結果: NPC2 は 人狼 です。", created_at=10,
        )
    )
    await fresh_repo.insert_log_private(
        LogEntry(
            game_id=g.id, day=2, phase=Phase.NIGHT,
            kind="SEER_RESULT", actor_seat=None, audience_seat=5,
            visibility="PRIVATE",
            text="占い結果: NPC3 は 人狼ではありません。", created_at=20,
        )
    )
    seers, _m, _g, _w, _a = await load_private_state_for_seat(
        fresh_repo, game_id=g.id, seat_no=5, role=Role.SEER,
        players=await fresh_repo.load_players(g.id),
        seats=await fresh_repo.load_seats(g.id),
    )
    by_day = {s.day: s for s in seers}
    assert by_day[1].target_seat == 2 and by_day[1].is_wolf is True
    assert by_day[2].target_seat == 3 and by_day[2].is_wolf is False


async def test_load_private_state_wolf_chat_history_for_wolf(
    fresh_repo: SqliteRepo,
) -> None:
    """Wolf NPCs see WOLF_CHAT private logs (audience_seat=NULL)."""
    g = await _seed_game_with_seer(fresh_repo)
    await fresh_repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await fresh_repo.set_player_role(g.id, 3, Role.WEREWOLF)
    await fresh_repo.insert_log_private(
        LogEntry(
            game_id=g.id, day=0, phase=Phase.NIGHT_0,
            kind="WOLF_CHAT", actor_seat=3, audience_seat=None,
            visibility="PRIVATE", text="今日は席5を狙うか", created_at=5,
        )
    )
    _s, _m, _gh, wolves, _a = await load_private_state_for_seat(
        fresh_repo, game_id=g.id, seat_no=1, role=Role.WEREWOLF,
        players=await fresh_repo.load_players(g.id),
        seats=await fresh_repo.load_seats(g.id),
    )
    assert len(wolves) == 1
    assert wolves[0].speaker_seat == 3
    assert wolves[0].text == "今日は席5を狙うか"


async def test_load_private_state_knight_guard_history_with_morning(
    fresh_repo: SqliteRepo,
) -> None:
    """Knight's guard history is rebuilt from night_actions, with
    peaceful_morning derived from the next day's MORNING public log."""
    g = await _seed_game_with_seer(fresh_repo)
    await fresh_repo.set_player_role(g.id, 7, Role.KNIGHT)
    await fresh_repo.insert_night_action(
        NightAction(
            game_id=g.id, day=1, actor_seat=7,
            kind=SubmissionType.KNIGHT_GUARD, target_seat=5,
            submitted_at=100,
        )
    )
    await fresh_repo.insert_log_public(
        LogEntry(
            game_id=g.id, day=2, phase=Phase.DAY_DISCUSSION,
            kind="MORNING", actor_seat=None, visibility="PUBLIC",
            text="平和な朝です。昨晩の犠牲者はいません。", created_at=200,
        )
    )
    _s, _m, guards, _w, _a = await load_private_state_for_seat(
        fresh_repo, game_id=g.id, seat_no=7, role=Role.KNIGHT,
        players=await fresh_repo.load_players(g.id),
        seats=await fresh_repo.load_seats(g.id),
    )
    assert len(guards) == 1
    assert guards[0].day == 1
    assert guards[0].target_seat == 5
    assert guards[0].peaceful_morning is True


async def test_load_private_state_wolf_attack_history_with_morning(
    fresh_repo: SqliteRepo,
) -> None:
    """Wolves see their own attack history with `peaceful_morning`
    derived from the matching MORNING public log so they can detect
    "GJ → re-attack the same target tomorrow because the knight can't
    guard consecutively".
    """
    g = await _seed_game_with_seer(fresh_repo)
    await fresh_repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await fresh_repo.set_player_role(g.id, 3, Role.WEREWOLF)
    # Wolf attack on day 1 — GJ'd by knight (peaceful morning on day 1).
    await fresh_repo.insert_night_action(
        NightAction(
            game_id=g.id, day=1, actor_seat=1,
            kind=SubmissionType.WOLF_ATTACK, target_seat=9,
            submitted_at=100,
        )
    )
    await fresh_repo.insert_log_public(
        LogEntry(
            game_id=g.id, day=1, phase=Phase.DAY_DISCUSSION,
            kind="MORNING", actor_seat=None, visibility="PUBLIC",
            text="平和な朝です。昨晩の犠牲者はいません。", created_at=200,
        )
    )
    # Wolf attack on day 2 — successful kill.
    await fresh_repo.insert_night_action(
        NightAction(
            game_id=g.id, day=2, actor_seat=1,
            kind=SubmissionType.WOLF_ATTACK, target_seat=5,
            submitted_at=300,
        )
    )
    await fresh_repo.insert_log_public(
        LogEntry(
            game_id=g.id, day=2, phase=Phase.DAY_DISCUSSION,
            kind="MORNING", actor_seat=5, visibility="PUBLIC",
            text="朝になりました。犠牲者: NPC5", created_at=400,
        )
    )

    _s, _m, _gh, _w, attacks = await load_private_state_for_seat(
        fresh_repo, game_id=g.id, seat_no=1, role=Role.WEREWOLF,
        players=await fresh_repo.load_players(g.id),
        seats=await fresh_repo.load_seats(g.id),
    )
    by_day = {a.day: a for a in attacks}
    assert by_day[1].target_seat == 9
    assert by_day[1].peaceful_morning is True, (
        "GJ on day 1 → wolves should see peaceful_morning=True so the "
        "next-night re-attack heuristic can fire"
    )
    assert by_day[2].target_seat == 5
    assert by_day[2].peaceful_morning is False
