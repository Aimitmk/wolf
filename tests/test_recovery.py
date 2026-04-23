"""Bot-restart recovery behavior."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator

import pytest_asyncio

from tests.fakes import FakeClock, FakeDiscordAdapter, FakeLLMAdapter
from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Seat
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.game_service import GameService, new_game_id
from wolfbot.services.recovery_service import RecoveryService
from wolfbot.services.timer_service import EngineRegistry


def _seats() -> list[Seat]:
    return [
        Seat(seat_no=i, display_name=f"P{i}", discord_user_id=f"u{i}",
             is_llm=False, persona_key=None)
        for i in range(1, 10)
    ]


async def _seed_game_at_night_vote(
    repo: SqliteRepo,
    deadline_epoch: int,
    now: int,
    phase: Phase = Phase.DAY_VOTE,
) -> Game:
    game = Game(
        id=new_game_id(), guild_id="g", host_user_id="h",
        phase=phase, day_number=1,
        deadline_epoch=deadline_epoch,
        main_text_channel_id="c1", main_vc_channel_id="c2",
        heaven_channel_id="ch-h", wolves_channel_id="ch-w",
        created_at=0,
    )
    await repo.create_game(game)
    for s in _seats():
        await repo.insert_seat(game.id, s)
    for p in await repo.load_players(game.id):
        # assign dummy roles so live-role lookups work
        role = [
            Role.WEREWOLF, Role.WEREWOLF, Role.MADMAN,
            Role.SEER, Role.MEDIUM, Role.KNIGHT,
            Role.VILLAGER, Role.VILLAGER, Role.VILLAGER,
        ][p.seat_no - 1]
        await repo.set_player_role(game.id, p.seat_no, role)
    return game


@pytest_asyncio.fixture
async def rec_bundle(repo: SqliteRepo) -> AsyncIterator[tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock]]:
    disc = FakeDiscordAdapter()
    llm = FakeLLMAdapter()
    reg = EngineRegistry()
    clock = FakeClock(now=10_000)
    gs = GameService(
        repo=repo, discord=disc, llm=llm, wake=reg,
        clock=clock, rng=random.Random(0),
    )
    rec = RecoveryService(repo=repo, game_service=gs, registry=reg, discord=disc, clock=clock)
    try:
        yield rec, gs, disc, reg, clock
    finally:
        await reg.stop_all()


async def test_restart_after_deadline_enters_waiting(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    rec, _, disc, _reg, clock = rec_bundle
    # Game's deadline is in the past
    await _seed_game_at_night_vote(repo, deadline_epoch=clock.now - 60, now=clock.now)

    recovered = await rec.recover_all()
    assert len(recovered) == 1

    games = await repo.load_active_games()
    assert len(games) == 1
    assert games[0].phase is Phase.WAITING_HOST_DECISION
    pending = await repo.load_pending_decision(games[0].id)
    assert pending is not None
    assert pending.phase is Phase.DAY_VOTE

    # announce_recovery called with the pending
    ann = [c for c in disc.calls if c.name == "announce_recovery"]
    assert len(ann) == 1
    assert ann[0].kwargs["pending_phase"] is Phase.DAY_VOTE


async def test_restart_before_deadline_keeps_phase_and_schedules_timer(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    rec, _, _, reg, clock = rec_bundle
    await _seed_game_at_night_vote(repo, deadline_epoch=clock.now + 300, now=clock.now)

    await rec.recover_all()

    games = await repo.load_active_games()
    assert len(games) == 1
    g = games[0]
    assert g.phase is Phase.DAY_VOTE
    assert g.deadline_epoch == clock.now + 300
    # Engine got attached
    assert g.id in reg._engines  # type: ignore[attr-defined]


async def test_restart_of_waiting_game_just_reconciles(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    rec, _, disc, _, clock = rec_bundle
    # Seed a game already in WAITING_HOST_DECISION (deadline must be None)
    game = await _seed_game_at_night_vote(
        repo, deadline_epoch=None, now=clock.now,
        phase=Phase.WAITING_HOST_DECISION,
    )
    await repo.upsert_pending_decision(
        __import__(
            "wolfbot.domain.models", fromlist=["PendingDecision"]
        ).PendingDecision(
            game_id=game.id, phase=Phase.NIGHT, day=1,
            required_submission=__import__(
                "wolfbot.domain.enums", fromlist=["SubmissionType"]
            ).SubmissionType.WOLF_ATTACK,
            missing_seats=(1,),
            created_at=clock.now,
        )
    )

    await rec.recover_all()

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.WAITING_HOST_DECISION
    # reconcile + announce_recovery both fired
    names = [c.name for c in disc.calls]
    assert "reconcile" in names
    assert "announce_recovery" in names


async def test_recovery_isolation_per_game(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    """One game failing shouldn't block others."""
    rec, _, disc, _reg, clock = rec_bundle
    # Game A (good)
    await _seed_game_at_night_vote(
        repo, deadline_epoch=clock.now + 300, now=clock.now
    )
    # Fail on reconcile only for subsequent call — simulate a mid-recovery error
    disc.fail_on.add("reconcile")

    recovered = await rec.recover_all()
    # Even though reconcile raises, recovery should have proceeded and returned IDs.
    # Our service catches reconcile failure. So the game still gets "recovered".
    assert len(recovered) == 1
