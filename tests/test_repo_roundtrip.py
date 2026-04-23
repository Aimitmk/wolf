"""SqliteRepo write/read roundtrip coverage."""

from __future__ import annotations

from wolfbot.domain.enums import (
    DeathCause,
    Phase,
    Role,
    SubmissionType,
)
from wolfbot.domain.models import (
    Game,
    LogEntry,
    NightAction,
    PendingDecision,
    PlayerUpdate,
    Seat,
    Transition,
    Vote,
)
from wolfbot.persistence.sqlite_repo import SqliteRepo


async def _base_game(repo: SqliteRepo, seats: list[Seat]) -> Game:
    game = Game(
        id="gm-1",
        guild_id="guild-1",
        host_user_id="host-1",
        phase=Phase.LOBBY,
        day_number=0,
        deadline_epoch=None,
        main_text_channel_id="c-text",
        main_vc_channel_id="c-vc",
        created_at=1000,
    )
    await repo.create_game(game)
    for s in seats:
        await repo.insert_seat(game.id, s)
    return game


async def test_game_roundtrip(repo: SqliteRepo, seats: list[Seat]) -> None:
    g = await _base_game(repo, seats)
    loaded = await repo.load_game(g.id)
    assert loaded is not None
    assert loaded.id == g.id
    assert loaded.phase is Phase.LOBBY
    assert loaded.day_number == 0
    assert loaded.deadline_epoch is None
    assert loaded.force_skip_pending is False


async def test_seats_roundtrip(repo: SqliteRepo, seats: list[Seat]) -> None:
    g = await _base_game(repo, seats)
    loaded = await repo.load_seats(g.id)
    assert len(loaded) == 9
    assert loaded[0].display_name == "Human1"
    assert loaded[5].is_llm is True
    assert loaded[5].persona_key == "setsu"


async def test_vote_upsert(repo: SqliteRepo, seats: list[Seat]) -> None:
    g = await _base_game(repo, seats)
    v1 = Vote(game_id=g.id, day=1, round=0, voter_seat=1, target_seat=5, submitted_at=10)
    await repo.insert_vote(v1)
    # Overwrite same voter (revote)
    v2 = Vote(game_id=g.id, day=1, round=0, voter_seat=1, target_seat=7, submitted_at=11)
    await repo.insert_vote(v2)
    loaded = await repo.load_votes(g.id, day=1, round_=0)
    assert len(loaded) == 1
    assert loaded[0].target_seat == 7
    assert loaded[0].submitted_at == 11


async def test_night_action_upsert(repo: SqliteRepo, seats: list[Seat]) -> None:
    g = await _base_game(repo, seats)
    a = NightAction(
        game_id=g.id, day=1, actor_seat=1,
        kind=SubmissionType.WOLF_ATTACK, target_seat=5, submitted_at=20,
    )
    await repo.insert_night_action(a)
    a2 = a.model_copy(update={"target_seat": 6, "submitted_at": 21})
    await repo.insert_night_action(a2)
    loaded = await repo.load_night_actions(g.id, day=1)
    assert len(loaded) == 1
    assert loaded[0].target_seat == 6


async def test_pending_decision_roundtrip(repo: SqliteRepo, seats: list[Seat]) -> None:
    g = await _base_game(repo, seats)
    pd = PendingDecision(
        game_id=g.id, phase=Phase.DAY_VOTE, day=1,
        required_submission=SubmissionType.VOTE,
        missing_seats=(3, 5, 7), created_at=30,
    )
    await repo.upsert_pending_decision(pd)
    loaded = await repo.load_pending_decision(g.id)
    assert loaded is not None
    assert loaded.missing_seats == (3, 5, 7)
    await repo.clear_pending_decision(g.id)
    assert await repo.load_pending_decision(g.id) is None


async def test_previous_guard_roundtrip(repo: SqliteRepo, seats: list[Seat]) -> None:
    g = await _base_game(repo, seats)
    await repo.upsert_previous_guard(g.id, knight_seat=6, last_guard_seat=None, last_guard_day=0)
    await repo.upsert_previous_guard(g.id, knight_seat=6, last_guard_seat=3, last_guard_day=1)
    loaded = await repo.load_previous_guard(g.id)
    assert loaded == (6, 3, 1)


async def test_apply_transition_commits_all(repo: SqliteRepo, seats: list[Seat]) -> None:
    g = await _base_game(repo, seats)
    t = Transition(
        next_phase=Phase.SETUP,
        next_day=0,
        new_deadline_epoch=None,
        player_updates=(PlayerUpdate(seat_no=1, role=Role.WEREWOLF),),
        public_logs=(
            LogEntry(
                game_id=g.id, day=0, phase=Phase.LOBBY, kind="PHASE_CHANGE",
                actor_seat=None, visibility="PUBLIC",
                text="setup begin", created_at=100,
            ),
        ),
        private_logs=(
            LogEntry(
                game_id=g.id, day=0, phase=Phase.LOBBY, kind="ROLE_NOTICE",
                actor_seat=None, visibility="PRIVATE", audience_seat=1,
                text="あなたは人狼です", created_at=100,
            ),
        ),
    )
    ok = await repo.apply_transition(g.id, t, expected_phase=Phase.LOBBY)
    assert ok is True
    loaded = await repo.load_game(g.id)
    assert loaded is not None
    assert loaded.phase is Phase.SETUP
    seat1_roles = [p for p in await repo.load_players(g.id) if p.seat_no == 1]
    assert seat1_roles[0].role is Role.WEREWOLF
    pub_logs = await repo.load_public_logs(g.id)
    assert any(log["kind"] == "PHASE_CHANGE" for log in pub_logs)
    priv_logs = await repo.load_private_logs_for_audience(g.id, audience_seat=1)
    assert any(log["kind"] == "ROLE_NOTICE" for log in priv_logs)


async def test_apply_transition_optimistic_lock(repo: SqliteRepo, seats: list[Seat]) -> None:
    g = await _base_game(repo, seats)
    # expected_phase mismatch → returns False, DB unchanged
    t = Transition(next_phase=Phase.NIGHT_0, next_day=0)
    ok = await repo.apply_transition(g.id, t, expected_phase=Phase.NIGHT)
    assert ok is False
    loaded = await repo.load_game(g.id)
    assert loaded is not None
    assert loaded.phase is Phase.LOBBY


async def test_apply_transition_writes_pending(repo: SqliteRepo, seats: list[Seat]) -> None:
    g = await _base_game(repo, seats)
    pd = PendingDecision(
        game_id=g.id, phase=Phase.DAY_VOTE, day=1,
        required_submission=SubmissionType.VOTE,
        missing_seats=(2, 4), created_at=40,
    )
    t = Transition(
        next_phase=Phase.WAITING_HOST_DECISION, next_day=1,
        requires_host_decision=True, pending=pd,
    )
    assert await repo.apply_transition(g.id, t, expected_phase=Phase.LOBBY)
    loaded = await repo.load_pending_decision(g.id)
    assert loaded is not None
    assert loaded.missing_seats == (2, 4)


async def test_apply_transition_kills_player_and_records_guard(
    repo: SqliteRepo, seats: list[Seat]
) -> None:
    g = await _base_game(repo, seats)
    t = Transition(
        next_phase=Phase.DAY_DISCUSSION,
        next_day=2,
        player_updates=(
            PlayerUpdate(
                seat_no=5, alive=False, death_cause=DeathCause.ATTACK, death_day=1,
            ),
        ),
        record_guard=(6, 3),
    )
    assert await repo.apply_transition(g.id, t, expected_phase=Phase.LOBBY)
    players = await repo.load_players(g.id)
    dead = next(p for p in players if p.seat_no == 5)
    assert dead.alive is False
    assert dead.death_cause is DeathCause.ATTACK
    assert dead.death_day == 1
    assert await repo.load_previous_guard(g.id) == (6, 3, 2)


async def test_active_game_lookup(repo: SqliteRepo, seats: list[Seat]) -> None:
    g = await _base_game(repo, seats)
    active = await repo.load_active_games()
    assert len(active) == 1
    found = await repo.load_active_game_for_guild("guild-1")
    assert found is not None and found.id == g.id

    # End game — no longer active
    await repo.end_game(g.id, ended_at_epoch=99999)
    assert await repo.load_active_games() == []
    assert await repo.load_active_game_for_guild("guild-1") is None


async def test_persona_assignments(repo: SqliteRepo, seats: list[Seat]) -> None:
    g = await _base_game(repo, seats)
    await repo.insert_persona_assignment(g.id, 6, "setsu")
    await repo.insert_persona_assignment(g.id, 7, "gina")
    keys = await repo.load_persona_keys(g.id)
    assert keys == {6: "setsu", 7: "gina"}


async def test_llm_speech_count(repo: SqliteRepo, seats: list[Seat]) -> None:
    g = await _base_game(repo, seats)
    await repo.increment_llm_normal_speech(g.id, day=1, seat_no=6, now_epoch=500)
    await repo.increment_llm_normal_speech(g.id, day=1, seat_no=6, now_epoch=520)
    c, vote_done, last = await repo.load_llm_speech(g.id, day=1, seat_no=6)
    assert c == 2
    assert vote_done is False
    assert last == 520
    await repo.mark_llm_vote_intent(g.id, day=1, seat_no=6)
    _, vote_done, _ = await repo.load_llm_speech(g.id, day=1, seat_no=6)
    assert vote_done is True
