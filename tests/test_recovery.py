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
        Seat(
            seat_no=i, display_name=f"P{i}", discord_user_id=f"u{i}", is_llm=False, persona_key=None
        )
        for i in range(1, 10)
    ]


async def _seed_game_at_night_vote(
    repo: SqliteRepo,
    deadline_epoch: int,
    now: int,
    phase: Phase = Phase.DAY_VOTE,
) -> Game:
    game = Game(
        id=new_game_id(),
        guild_id="g",
        host_user_id="h",
        phase=phase,
        day_number=1,
        deadline_epoch=deadline_epoch,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        heaven_channel_id="ch-h",
        wolves_channel_id="ch-w",
        created_at=0,
    )
    await repo.create_game(game)
    for s in _seats():
        await repo.insert_seat(game.id, s)
    for p in await repo.load_players(game.id):
        # assign dummy roles so live-role lookups work
        role = [
            Role.WEREWOLF,
            Role.WEREWOLF,
            Role.MADMAN,
            Role.SEER,
            Role.MEDIUM,
            Role.KNIGHT,
            Role.VILLAGER,
            Role.VILLAGER,
            Role.VILLAGER,
        ][p.seat_no - 1]
        await repo.set_player_role(game.id, p.seat_no, role)
    return game


@pytest_asyncio.fixture
async def rec_bundle(
    repo: SqliteRepo,
) -> AsyncIterator[
    tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock]
]:
    disc = FakeDiscordAdapter()
    llm = FakeLLMAdapter()
    reg = EngineRegistry()
    clock = FakeClock(now=10_000)
    gs = GameService(
        repo=repo,
        discord=disc,
        llm=llm,
        wake=reg,
        clock=clock,
        rng=random.Random(0),
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


async def test_restart_before_deadline_resends_pending_dms(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    """Live DAY_VOTE phase post-restart must re-send DMs to still-missing voters."""
    from wolfbot.domain.models import Vote

    rec, _, disc, _, clock = rec_bundle
    game = await _seed_game_at_night_vote(
        repo, deadline_epoch=clock.now + 300, now=clock.now, phase=Phase.DAY_VOTE
    )
    # Seats 1 and 2 already voted before the crash.
    for seat in (1, 2):
        await repo.insert_vote(
            Vote(
                game_id=game.id,
                day=1,
                round=0,
                voter_seat=seat,
                target_seat=5,
                submitted_at=clock.now - 60,
            )
        )

    await rec.recover_all()

    sent = [c for c in disc.calls if c.name == "send_vote_dms"]
    assert len(sent) == 1
    dmed = set(sent[0].kwargs["voters"])
    # Seats 1 and 2 already submitted, so they must not be re-DMed.
    assert dmed == {3, 4, 5, 6, 7, 8, 9}


async def test_restart_waiting_phase_does_not_resend(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    """If the deadline had already expired, we go into WAITING and the host must
    drive the resume via /wolf extend — recovery itself should not resend DMs."""
    rec, _, disc, _, clock = rec_bundle
    await _seed_game_at_night_vote(
        repo, deadline_epoch=clock.now - 60, now=clock.now, phase=Phase.DAY_VOTE
    )

    await rec.recover_all()

    assert not any(c.name == "send_vote_dms" for c in disc.calls)


async def test_restart_of_waiting_game_just_reconciles(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    rec, _, disc, _, clock = rec_bundle
    # Seed a game already in WAITING_HOST_DECISION (deadline must be None)
    game = await _seed_game_at_night_vote(
        repo,
        deadline_epoch=None,
        now=clock.now,
        phase=Phase.WAITING_HOST_DECISION,
    )
    await repo.upsert_pending_decision(
        __import__("wolfbot.domain.models", fromlist=["PendingDecision"]).PendingDecision(
            game_id=game.id,
            phase=Phase.NIGHT,
            day=1,
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
    await _seed_game_at_night_vote(repo, deadline_epoch=clock.now + 300, now=clock.now)
    # Fail on reconcile only for subsequent call — simulate a mid-recovery error
    disc.fail_on.add("reconcile")

    recovered = await rec.recover_all()
    # Even though reconcile raises, recovery should have proceeded and returned IDs.
    # Our service catches reconcile failure. So the game still gets "recovered".
    assert len(recovered) == 1


async def test_double_recover_does_not_orphan_engines(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    """Simulates on_ready re-firing after a reconnect. The second recover_all()
    must not leave the first engine task running."""
    rec, _, _, reg, clock = rec_bundle
    game = await _seed_game_at_night_vote(repo, deadline_epoch=clock.now + 300, now=clock.now)

    await rec.recover_all()
    first_engine = reg._engines[game.id]  # type: ignore[attr-defined]
    first_task = first_engine._task  # type: ignore[attr-defined]
    assert first_task is not None

    await rec.recover_all()
    second_engine = reg._engines[game.id]  # type: ignore[attr-defined]

    assert second_engine is not first_engine
    # Previous engine's task must have been stopped during re-attach
    assert first_task.done()
    # Registry still holds exactly one engine per game
    assert len(reg._engines) == 1  # type: ignore[attr-defined]


async def test_recovery_pending_day_vote_excludes_already_voted(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    """DAY_VOTE: only seats without a submitted vote should be in missing_seats."""
    from wolfbot.domain.models import Vote

    rec, _, _, _, clock = rec_bundle
    game = await _seed_game_at_night_vote(
        repo, deadline_epoch=clock.now - 60, now=clock.now, phase=Phase.DAY_VOTE
    )
    # Seats 1, 2, 3 have already voted.
    for seat in (1, 2, 3):
        await repo.insert_vote(
            Vote(
                game_id=game.id,
                day=1,
                round=0,
                voter_seat=seat,
                target_seat=4,
                submitted_at=clock.now - 120,
            )
        )

    await rec.recover_all()
    pending = await repo.load_pending_decision(game.id)
    assert pending is not None
    assert pending.missing_seats == (4, 5, 6, 7, 8, 9)
    # submissions breakdown reflects the same
    assert len(pending.submissions) == 1
    assert pending.submissions[0].missing_seats == (4, 5, 6, 7, 8, 9)


async def test_recovery_pending_night_excludes_knight_on_day_zero(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    """Night 0: knight doesn't act, so they must not be in submissions."""
    from wolfbot.domain.enums import SubmissionType

    rec, _, _, _, clock = rec_bundle
    # Seed game at NIGHT day 0
    game = Game(
        id=new_game_id(),
        guild_id="g",
        host_user_id="h",
        phase=Phase.NIGHT,
        day_number=0,
        deadline_epoch=clock.now - 60,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        heaven_channel_id="ch-h",
        wolves_channel_id="ch-w",
        created_at=0,
    )
    await repo.create_game(game)
    for s in _seats():
        await repo.insert_seat(game.id, s)
    roles = [
        Role.WEREWOLF,
        Role.WEREWOLF,
        Role.MADMAN,
        Role.SEER,
        Role.MEDIUM,
        Role.KNIGHT,
        Role.VILLAGER,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    for idx, role in enumerate(roles, start=1):
        await repo.set_player_role(game.id, idx, role)

    await rec.recover_all()
    pending = await repo.load_pending_decision(game.id)
    assert pending is not None
    submission_types = {s.submission_type for s in pending.submissions}
    assert SubmissionType.KNIGHT_GUARD not in submission_types


async def test_recovery_pending_night_primary_is_first_outstanding(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    """If wolves submitted but seer hasn't, required_submission is SEER_DIVINE."""
    from wolfbot.domain.enums import SubmissionType
    from wolfbot.domain.models import NightAction

    rec, _, _, _, clock = rec_bundle
    # Night day 1 — knight is expected too.
    game = Game(
        id=new_game_id(),
        guild_id="g",
        host_user_id="h",
        phase=Phase.NIGHT,
        day_number=1,
        deadline_epoch=clock.now - 60,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        heaven_channel_id="ch-h",
        wolves_channel_id="ch-w",
        created_at=0,
    )
    await repo.create_game(game)
    for s in _seats():
        await repo.insert_seat(game.id, s)
    roles = [
        Role.WEREWOLF,
        Role.WEREWOLF,
        Role.MADMAN,
        Role.SEER,
        Role.MEDIUM,
        Role.KNIGHT,
        Role.VILLAGER,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    for idx, role in enumerate(roles, start=1):
        await repo.set_player_role(game.id, idx, role)
    # Both wolves submit their attack; seer and knight do not.
    for wolf_seat in (1, 2):
        await repo.insert_night_action(
            NightAction(
                game_id=game.id,
                day=1,
                actor_seat=wolf_seat,
                kind=SubmissionType.WOLF_ATTACK,
                target_seat=7,
                submitted_at=clock.now - 120,
            )
        )

    await rec.recover_all()
    pending = await repo.load_pending_decision(game.id)
    assert pending is not None
    # WOLF_ATTACK is satisfied → primary shifts to SEER_DIVINE
    assert pending.required_submission is SubmissionType.SEER_DIVINE
    kinds = [s.submission_type for s in pending.submissions]
    assert kinds == [SubmissionType.SEER_DIVINE, SubmissionType.KNIGHT_GUARD]
    seer_sub = next(
        s for s in pending.submissions if s.submission_type is SubmissionType.SEER_DIVINE
    )
    assert seer_sub.missing_seats == (4,)
    knight_sub = next(
        s for s in pending.submissions if s.submission_type is SubmissionType.KNIGHT_GUARD
    )
    assert knight_sub.missing_seats == (6,)


async def test_recovery_pending_night_split_wolves_are_unresolved(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    """Wolves who submitted different attack targets must be captured as
    `unresolved_seats` (not left with empty missing/unresolved like the old
    snapshot that only looked at who had a row in night_actions)."""
    from wolfbot.domain.enums import SubmissionType
    from wolfbot.domain.models import NightAction

    rec, _, _, _, clock = rec_bundle
    game = Game(
        id=new_game_id(),
        guild_id="g",
        host_user_id="h",
        phase=Phase.NIGHT,
        day_number=1,
        deadline_epoch=clock.now - 60,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        heaven_channel_id="ch-h",
        wolves_channel_id="ch-w",
        created_at=0,
    )
    await repo.create_game(game)
    for s in _seats():
        await repo.insert_seat(game.id, s)
    roles = [
        Role.WEREWOLF,
        Role.WEREWOLF,
        Role.MADMAN,
        Role.SEER,
        Role.MEDIUM,
        Role.KNIGHT,
        Role.VILLAGER,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    for idx, role in enumerate(roles, start=1):
        await repo.set_player_role(game.id, idx, role)
    # Split: wolf 1 targets seat 7, wolf 2 targets seat 8. Seer and knight submit.
    await repo.insert_night_action(
        NightAction(
            game_id=game.id,
            day=1,
            actor_seat=1,
            kind=SubmissionType.WOLF_ATTACK,
            target_seat=7,
            submitted_at=clock.now - 120,
        )
    )
    await repo.insert_night_action(
        NightAction(
            game_id=game.id,
            day=1,
            actor_seat=2,
            kind=SubmissionType.WOLF_ATTACK,
            target_seat=8,
            submitted_at=clock.now - 120,
        )
    )
    await repo.insert_night_action(
        NightAction(
            game_id=game.id,
            day=1,
            actor_seat=4,
            kind=SubmissionType.SEER_DIVINE,
            target_seat=3,
            submitted_at=clock.now - 120,
        )
    )
    await repo.insert_night_action(
        NightAction(
            game_id=game.id,
            day=1,
            actor_seat=6,
            kind=SubmissionType.KNIGHT_GUARD,
            target_seat=3,
            submitted_at=clock.now - 120,
        )
    )

    await rec.recover_all()

    pending = await repo.load_pending_decision(game.id)
    assert pending is not None
    assert pending.required_submission is SubmissionType.WOLF_ATTACK
    wolf_sub = next(
        s for s in pending.submissions if s.submission_type is SubmissionType.WOLF_ATTACK
    )
    assert wolf_sub.missing_seats == ()
    assert wolf_sub.unresolved_seats == (1, 2)
    # `missing_seats` on the legacy summary surfaces both wolves as needing action.
    assert set(pending.missing_seats) == {1, 2}


async def test_host_extend_resends_dms_to_split_wolves(
    repo: SqliteRepo,
    rec_bundle: tuple[RecoveryService, GameService, FakeDiscordAdapter, EngineRegistry, FakeClock],
) -> None:
    """After a split has parked the game in WAITING_HOST_DECISION, `/wolf extend`
    must re-send night action DMs to the split wolves so they can converge on a
    common target."""
    from wolfbot.domain.enums import SubmissionType
    from wolfbot.domain.models import NightAction

    rec, gs, disc, _, clock = rec_bundle
    game = Game(
        id=new_game_id(),
        guild_id="g",
        host_user_id="h",
        phase=Phase.NIGHT,
        day_number=1,
        deadline_epoch=clock.now - 60,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        heaven_channel_id="ch-h",
        wolves_channel_id="ch-w",
        created_at=0,
    )
    await repo.create_game(game)
    for s in _seats():
        await repo.insert_seat(game.id, s)
    roles = [
        Role.WEREWOLF,
        Role.WEREWOLF,
        Role.MADMAN,
        Role.SEER,
        Role.MEDIUM,
        Role.KNIGHT,
        Role.VILLAGER,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    for idx, role in enumerate(roles, start=1):
        await repo.set_player_role(game.id, idx, role)
    # Split: wolf 1 → 7, wolf 2 → 8. Seer + knight submitted too.
    submissions = [
        (1, SubmissionType.WOLF_ATTACK, 7),
        (2, SubmissionType.WOLF_ATTACK, 8),
        (4, SubmissionType.SEER_DIVINE, 3),
        (6, SubmissionType.KNIGHT_GUARD, 3),
    ]
    for actor, kind, target in submissions:
        await repo.insert_night_action(
            NightAction(
                game_id=game.id,
                day=1,
                actor_seat=actor,
                kind=kind,
                target_seat=target,
                submitted_at=clock.now - 120,
            )
        )

    await rec.recover_all()  # parks WAITING with unresolved wolves
    disc.reset()

    ok = await gs.host_extend(game.id, extra_seconds=60)
    assert ok

    sent = [c for c in disc.calls if c.name == "send_night_action_dms"]
    assert len(sent) == 1
    dmed = set(sent[0].kwargs["players"])
    # Both wolves are re-DMed so they can reselect a target
    assert 1 in dmed and 2 in dmed
    # Seer and knight already submitted — not in the resend
    assert 4 not in dmed and 6 not in dmed


async def test_pending_decision_backward_compat_synthesizes_submissions() -> None:
    """Old DB rows (no submissions_json) should still yield a single-entry breakdown."""
    from wolfbot.domain.enums import SubmissionType
    from wolfbot.domain.models import PendingDecision

    pd = PendingDecision(
        game_id="g",
        phase=Phase.DAY_VOTE,
        day=1,
        required_submission=SubmissionType.VOTE,
        missing_seats=(3, 5),
        submissions=(),
        created_at=0,
    )
    synth = pd.effective_submissions()
    assert len(synth) == 1
    assert synth[0].submission_type is SubmissionType.VOTE
    assert synth[0].missing_seats == (3, 5)


async def test_engine_registry_attach_stops_existing(
    repo: SqliteRepo,
) -> None:
    """attach() must stop any prior engine sharing the same game_id."""
    import asyncio

    from wolfbot.services.timer_service import GameEngine

    reg = EngineRegistry()
    game = Game(
        id=new_game_id(),
        guild_id="g",
        host_user_id="h",
        phase=Phase.DAY_VOTE,
        day_number=1,
        deadline_epoch=10**10,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(game)

    async def noop(_: str) -> None:
        return None

    first = GameEngine(game_id=game.id, repo=repo, advance=noop)
    await reg.attach(first)
    first.start()
    await asyncio.sleep(0)  # let the task actually start

    second = GameEngine(game_id=game.id, repo=repo, advance=noop)
    await reg.attach(second)

    assert reg._engines[game.id] is second  # type: ignore[attr-defined]
    # First engine's task has been stopped
    assert first._task is not None and first._task.done()  # type: ignore[attr-defined]

    # Cleanup — end the game so second's loop exits, then stop_all.
    await repo.end_game(game.id, ended_at_epoch=1)
    second.wake()
    await reg.stop_all()
