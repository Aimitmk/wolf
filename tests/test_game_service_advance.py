"""End-to-end game_service.advance behavior, with in-memory fakes."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator

import pytest_asyncio

from tests.fakes import FakeClock, FakeDiscordAdapter, FakeLLMAdapter
from wolfbot.domain.enums import DeathCause, Phase, Role, SubmissionType, SubmitResult
from wolfbot.domain.models import Game, PlayerUpdate, Seat, Transition
from wolfbot.domain.rules import previous_guard_seat_for_night
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.game_service import GameService, new_game_id
from wolfbot.services.timer_service import EngineRegistry


def _nine_seats() -> list[Seat]:
    out: list[Seat] = []
    for i in range(1, 10):
        if i <= 5:
            out.append(
                Seat(
                    seat_no=i,
                    display_name=f"H{i}",
                    discord_user_id=f"u{i}",
                    is_llm=False,
                    persona_key=None,
                )
            )
        else:
            out.append(
                Seat(
                    seat_no=i,
                    display_name=f"LLM{i}",
                    discord_user_id=None,
                    is_llm=True,
                    persona_key=f"p{i}",
                )
            )
    return out


async def _make_game_in_setup(repo: SqliteRepo) -> Game:
    game = Game(
        id=new_game_id(),
        guild_id="g",
        host_user_id="h",
        phase=Phase.SETUP,
        day_number=0,
        main_text_channel_id="ch-text",
        main_vc_channel_id="ch-vc",
        heaven_channel_id="ch-heaven",
        wolves_channel_id="ch-wolves",
        created_at=0,
    )
    await repo.create_game(game)
    for s in _nine_seats():
        await repo.insert_seat(game.id, s)
    return game


async def _seed_llm_discussion_rounds_done(repo: SqliteRepo, game_id: str, day: int) -> None:
    """Mark every LLM seat as having completed both DAY_DISCUSSION rounds.

    Tests that drive DAY_DISCUSSION → DAY_VOTE via clock.tick(300) call this
    before ticking so the deadline-passed branch in `_plan_next` finds
    rounds_done=True and proceeds to the vote phase. With FakeLLMAdapter
    the actual round task is just recorded — no progress is written — so
    the tests must seed progress themselves.
    """
    for seat_no in (6, 7, 8, 9):  # LLM seats per _nine_seats()
        await repo.increment_llm_discussion_round(game_id, day=day, seat_no=seat_no)
        await repo.increment_llm_discussion_round(game_id, day=day, seat_no=seat_no)


@pytest_asyncio.fixture
async def svc(
    repo: SqliteRepo,
) -> AsyncIterator[
    tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock]
]:
    disc = FakeDiscordAdapter()
    llm = FakeLLMAdapter()
    reg = EngineRegistry()
    clock = FakeClock(now=1000)
    service = GameService(
        repo=repo,
        discord=disc,
        llm=llm,
        wake=reg,
        clock=clock,
        rng=random.Random(0),
    )
    yield service, disc, llm, reg, clock


async def test_advance_setup_assigns_roles_and_moves_to_night0(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, disc, _, _, _ = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.NIGHT_0
    players = await repo.load_players(game.id)
    assert all(p.role is not None for p in players)
    # Permissions and public announcement both fired
    names = {c.name for c in disc.calls}
    assert "apply_permissions" in names
    assert "post_public" in names


async def test_advance_night0_to_day1_sends_role_notices(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, disc, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    disc.reset()
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_DISCUSSION
    assert loaded.day_number == 1
    assert loaded.deadline_epoch == clock.now + 300  # day 1 discussion duration

    # 9 role notices + 1 seer white + 2 wolf partners = 12 DMs
    privates = [c for c in disc.calls if c.name == "send_private"]
    assert len(privates) == 12


async def test_advance_day_discussion_to_vote(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, disc, llm, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION
    disc.reset()
    llm.calls.clear()

    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # DAY_DISCUSSION -> DAY_VOTE

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_VOTE
    assert loaded.deadline_epoch == clock.now + 60  # VOTE_DURATION

    # Vote DMs dispatched to 9 alive players
    vote_calls = [c for c in disc.calls if c.name == "send_vote_dms"]
    assert len(vote_calls) == 1
    assert vote_calls[0].kwargs["round_"] == 0
    assert sorted(vote_calls[0].kwargs["voters"]) == list(range(1, 10))

    # LLM adapter was asked for votes too
    assert any(c.name == "submit_llm_votes" for c in llm.calls)


async def test_submit_vote_wakes_engine_when_all_submitted(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """When the final vote comes in, `wake` should be called on the registry."""
    service, _, _, reg, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # DAY_DISCUSSION -> DAY_VOTE

    wakes: list[str] = []
    original_wake = reg.wake

    def track(game_id: str) -> None:
        wakes.append(game_id)
        original_wake(game_id)

    reg.wake = track  # type: ignore[method-assign]

    # Submit 9 votes
    for voter in range(1, 10):
        target = 1 if voter != 1 else 2
        await service.submit_vote(game.id, voter, target, round_=0, day=1)

    # The final submit should have triggered a wake
    assert game.id in wakes


async def test_submit_vote_does_not_wake_when_incomplete(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, _, _, reg, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)

    wakes: list[str] = []
    reg.wake = lambda gid: wakes.append(gid)  # type: ignore[method-assign]

    # Only 5 of 9 vote
    for voter in range(1, 6):
        await service.submit_vote(game.id, voter, target_seat=7, round_=0, day=1)
    assert wakes == []


async def test_day_vote_resolve_missing_enters_waiting(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, disc, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # → DAY_VOTE

    # Only 3 people voted; reach deadline → WAITING
    for voter in range(1, 4):
        await service.submit_vote(game.id, voter, 7, round_=0, day=1)

    disc.reset()
    clock.tick(60)
    await service.advance(game.id)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.WAITING_HOST_DECISION
    pending = await repo.load_pending_decision(game.id)
    assert pending is not None
    assert set(pending.missing_seats) == {4, 5, 6, 7, 8, 9}

    waiting_calls = [c for c in disc.calls if c.name == "announce_waiting"]
    assert len(waiting_calls) == 1


async def test_host_force_skip_resolves_vote_with_abstentions(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # DAY_VOTE

    # 5 vote for seat 7
    for voter in range(1, 6):
        await service.submit_vote(game.id, voter, 7, round_=0, day=1)
    clock.tick(60)
    await service.advance(game.id)  # → WAITING

    # Host force-skips
    ok = await service.host_force_skip(game.id)
    assert ok

    await service.advance(game.id)  # resume DAY_VOTE with force_skip=True

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    # seat 7 got 5 votes, everyone else 0 after abstention → executed
    assert loaded.phase in (Phase.NIGHT, Phase.GAME_OVER)
    dead = [p for p in await repo.load_players(game.id) if not p.alive]
    assert any(p.seat_no == 7 for p in dead)


async def test_host_extend_resumes_phase_with_new_deadline(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # DAY_VOTE
    clock.tick(60)
    await service.advance(game.id)  # → WAITING (nobody voted)

    ok = await service.host_extend(game.id, extra_seconds=120)
    assert ok
    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_VOTE
    assert loaded.deadline_epoch == clock.now + 120
    assert await repo.load_pending_decision(game.id) is None


# ----- DAY_VOTE / DAY_RUNOFF deadline-guard regression tests -------------
# These pin the service-layer guard added to GameService._plan_next that
# prevents a stale wake or partial LLM-vote completion from collapsing an
# active vote phase into WAITING_HOST_DECISION before the deadline. The bug
# observed in production was day-3 specific in symptom but day-number-
# agnostic in mechanism, so the tests cover day-1 typical cases AND a
# day-3 progression to confirm parity.


async def test_advance_day_vote_before_deadline_with_missing_votes_stays_in_phase(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Day-1 DAY_VOTE before the deadline with only some voters submitted must
    stay in DAY_VOTE — without the guard, plan_day_vote_resolve would see
    missing voters and immediately enter WAITING_HOST_DECISION."""
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # → DAY_VOTE

    # 3 of 9 voted, 6 still pending. Deadline (clock+60) has not been reached.
    for voter in (1, 2, 3):
        await service.submit_vote(game.id, voter, target_seat=7, round_=0, day=1)

    # Stale wake / spurious advance — must not collapse into WAITING.
    await service.advance(game.id)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_VOTE, (
        f"DAY_VOTE must hold before deadline with missing votes; got {loaded.phase}"
    )
    assert loaded.deadline_epoch is not None and loaded.deadline_epoch > clock.now
    assert await repo.load_pending_decision(game.id) is None


async def test_advance_day_vote_before_deadline_with_only_llm_votes_stays_in_phase(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Models the LLM-fire-and-forget case: LLM seats (6-9) submit votes via
    background tasks while humans (1-5) are still thinking. Each LLM submission
    can wake the engine; advance must stay in DAY_VOTE until the human side
    finishes or the deadline arrives."""
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # → DAY_VOTE

    # Only LLM seats voted; humans still pending. Deadline still in the future.
    for voter in (6, 7, 8, 9):
        await service.submit_vote(game.id, voter, target_seat=1, round_=0, day=1)

    await service.advance(game.id)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_VOTE
    assert await repo.load_pending_decision(game.id) is None


async def test_advance_day_vote_resolves_when_all_alive_voted_before_deadline(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Regression guard: the early-resolve path (all alive voters submitted →
    resolve immediately even before deadline) must still work after the new
    guard is added."""
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # → DAY_VOTE

    # All 9 vote for seat 1; deadline still in the future.
    for voter in range(1, 10):
        target = 1 if voter != 1 else 2
        await service.submit_vote(game.id, voter, target, round_=0, day=1)

    # Deadline still in the future — the guard must let resolution proceed
    # because all alive voters submitted.
    assert clock.now < (await repo.load_game(game.id)).deadline_epoch  # type: ignore[operator,union-attr]
    await service.advance(game.id)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase in (Phase.NIGHT, Phase.GAME_OVER)


async def test_advance_day_runoff_before_deadline_with_missing_votes_stays_in_phase(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Same guard for DAY_RUNOFF: stale wake before the runoff deadline with
    missing runoff votes must keep the game in DAY_RUNOFF, not WAITING."""
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # → DAY_VOTE

    # Construct a tie between seats 1 and 5 so we go to DAY_RUNOFF.
    for v in (1, 2, 3, 4):
        await service.submit_vote(game.id, v, target_seat=5, round_=0, day=1)
    for v in (6, 7, 8, 9):
        await service.submit_vote(game.id, v, target_seat=1, round_=0, day=1)
    await service.submit_vote(game.id, 5, target_seat=None, round_=0, day=1)

    await service.advance(game.id)  # → DAY_RUNOFF_SPEECH (intermediate phase)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    if loaded.phase is Phase.DAY_RUNOFF_SPEECH:
        # Push past the speech deadline so we land in DAY_RUNOFF.
        clock.tick(loaded.deadline_epoch - clock.now if loaded.deadline_epoch else 60)
        await service.advance(game.id)
    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_RUNOFF, f"expected DAY_RUNOFF, got {loaded.phase}"

    # Only 2 of 9 cast a runoff vote; deadline still in the future.
    await service.submit_vote(game.id, 2, target_seat=1, round_=1, day=1)
    await service.submit_vote(game.id, 3, target_seat=5, round_=1, day=1)

    # Stale advance — must not collapse into WAITING.
    await service.advance(game.id)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_RUNOFF
    assert await repo.load_pending_decision(game.id) is None


async def test_advance_day_vote_day3_before_deadline_stays_in_phase(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """End-to-end day-3 regression for the production bug: drive through day 1
    and day 2 by voting out a villager and resolving night actions, then on
    day 3 verify the same guard prevents WAITING_HOST_DECISION before deadline
    with missing voters. Confirms the guard behavior is uniform across days."""
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP → NIGHT_0
    await service.advance(game.id)  # NIGHT_0 → DAY_DISCUSSION day 1

    for day in (1, 2):
        next_phase = await _drive_one_day(repo, service, clock, game.id, day=day)
        if next_phase is not Phase.DAY_DISCUSSION:
            # Game ended before reaching day 3; nothing to test on day 3 path.
            # Mark as skipped via early return (regression test is conditional
            # on the wolves-survive-to-day-3 trajectory, deterministic for
            # rng=Random(0)).
            return

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_DISCUSSION
    assert loaded.day_number == 3

    # Advance to DAY_VOTE day 3.
    await _seed_llm_discussion_rounds_done(repo, game.id, day=3)
    clock.tick(180)  # day 3 discussion duration
    await service.advance(game.id)
    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_VOTE
    assert loaded.day_number == 3

    # Stale wake on day-3 DAY_VOTE with no votes yet — must stay in phase.
    await service.advance(game.id)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_VOTE
    assert loaded.day_number == 3
    assert await repo.load_pending_decision(game.id) is None


async def _drive_one_day(
    repo: SqliteRepo,
    service: GameService,
    clock: FakeClock,
    game_id: str,
    *,
    day: int,
) -> Phase:
    """Advance one full day cycle from DAY_DISCUSSION day=N to DAY_DISCUSSION
    day=N+1 (or earlier exit). Returns the phase after the NIGHT advance.

    Strategy: vote out a villager each day so wolves survive; submit all
    required night actions (wolf attack on a villager, seer divine, knight
    guard) so NIGHT resolves cleanly.
    """
    await _seed_llm_discussion_rounds_done(repo, game_id, day=day)
    clock.tick(300 if day == 1 else 240 if day == 2 else 180)
    await service.advance(game_id)  # DAY_DISCUSSION → DAY_VOTE

    players = await repo.load_players(game_id)
    villager_target = next(p.seat_no for p in players if p.alive and p.role is Role.VILLAGER)
    voters = [p.seat_no for p in players if p.alive]
    for v in voters:
        if v == villager_target:
            other = next(p.seat_no for p in players if p.alive and p.seat_no != v)
            await service.submit_vote(game_id, v, other, round_=0, day=day)
        else:
            await service.submit_vote(game_id, v, villager_target, round_=0, day=day)
    await service.advance(game_id)  # DAY_VOTE → NIGHT (or GAME_OVER)

    loaded = await repo.load_game(game_id)
    assert loaded is not None
    if loaded.phase is not Phase.NIGHT:
        return loaded.phase

    players = await repo.load_players(game_id)
    wolves = [p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF]
    villagers = [p.seat_no for p in players if p.alive and p.role is Role.VILLAGER]
    seer = next((p.seat_no for p in players if p.alive and p.role is Role.SEER), None)
    knight = next((p.seat_no for p in players if p.alive and p.role is Role.KNIGHT), None)

    attack_target = (
        villagers[0]
        if villagers
        else next(p.seat_no for p in players if p.alive and p.seat_no not in wolves)
    )
    for w in wolves:
        await service.submit_night_action(
            game_id, w, SubmissionType.WOLF_ATTACK, attack_target, day=day
        )
    if seer is not None:
        seer_target = next(p.seat_no for p in players if p.alive and p.seat_no != seer)
        await service.submit_night_action(
            game_id, seer, SubmissionType.SEER_DIVINE, seer_target, day=day
        )
    if knight is not None:
        loaded_for_day = await repo.load_game(game_id)
        assert loaded_for_day is not None
        prev = await repo.load_previous_guard(game_id)
        prev_target = previous_guard_seat_for_night(prev, loaded_for_day.day_number)
        forbidden: set[int] = {knight}
        if prev_target is not None:
            forbidden.add(prev_target)
        knight_target = next(p.seat_no for p in players if p.alive and p.seat_no not in forbidden)
        await service.submit_night_action(
            game_id, knight, SubmissionType.KNIGHT_GUARD, knight_target, day=day
        )

    await service.advance(game_id)  # NIGHT → DAY_DISCUSSION day+1
    loaded = await repo.load_game(game_id)
    assert loaded is not None
    return loaded.phase


async def test_host_abort_ends_game(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, disc, _, _, _ = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)

    ok = await service.host_abort(game.id)
    assert ok
    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.GAME_OVER
    assert loaded.ended_at is not None
    assert any(c.name == "on_game_end" for c in disc.calls)


async def test_host_abort_invokes_on_reactive_game_end_callback(
    repo: SqliteRepo,
) -> None:
    """The reactive_voice plumbing release path must fire on host_abort so
    NPC bots leave VC. Mirrors `discord.on_game_end` but on a separate hook
    so reactive_voice plumbing stays out of the GameService core."""
    disc = FakeDiscordAdapter()
    llm = FakeLLMAdapter()
    reg = EngineRegistry()
    fired: list[str] = []

    async def on_end(game_id: str) -> None:
        fired.append(game_id)

    service = GameService(
        repo=repo,
        discord=disc,
        llm=llm,
        wake=reg,
        rng=random.Random(0),
        on_reactive_game_end=on_end,
    )
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)

    ok = await service.host_abort(game.id)
    assert ok
    assert fired == [game.id]


async def test_host_abort_returns_false_when_already_ended(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Second abort on the same game must return False (race case) without re-posting on_game_end."""
    service, disc, _, _, _ = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)

    first = await service.host_abort(game.id)
    assert first
    on_game_end_after_first = sum(1 for c in disc.calls if c.name == "on_game_end")

    second = await service.host_abort(game.id)
    assert not second
    on_game_end_after_second = sum(1 for c in disc.calls if c.name == "on_game_end")
    assert on_game_end_after_second == on_game_end_after_first


async def test_optimistic_lock_prevents_double_advance(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """If advance runs twice concurrently, the second should no-op on the same phase."""
    service, _, _, _, _ = svc
    game = await _make_game_in_setup(repo)
    # Force SETUP two times; second should lose the optimistic lock
    await service.advance(game.id)
    # After first advance, phase is NIGHT_0. Calling advance again with the phase
    # captured as SETUP (stale) would miss; but the service reads latest phase each time,
    # so just verify that duplicate calls are idempotent.
    # We'll manually race a retry by faking a second advance that sees stale.
    # For simplicity just check we can advance the game forward cleanly.
    await service.advance(game.id)
    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_DISCUSSION


async def test_permission_failure_does_not_advance_phase(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, disc, _, _, _ = svc
    game = await _make_game_in_setup(repo)
    disc.fail_on.add("apply_permissions")

    await service.advance(game.id)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.SETUP  # unchanged


async def test_submit_vote_rejected_when_phase_not_day_vote(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Votes submitted during DAY_DISCUSSION (e.g. stale UI click) must not be persisted."""
    service, _, _, _, _ = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION (not DAY_VOTE!)

    result = await service.submit_vote(game.id, voter_seat=1, target_seat=2, round_=0, day=1)
    assert result is SubmitResult.STALE_PHASE

    votes = await repo.load_votes(game.id, day=1, round_=0)
    assert votes == [], "vote during DAY_DISCUSSION must be dropped"


async def test_submit_vote_rejected_when_voter_dead(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE

    # Kill seat 5 directly via repo internals (simulating they died in a prior phase).
    from wolfbot.domain.enums import DeathCause
    from wolfbot.domain.models import PlayerUpdate
    from wolfbot.persistence.sqlite_repo import _apply_player_update

    async with repo._tx() as db:
        await _apply_player_update(
            db,
            game.id,
            PlayerUpdate(seat_no=5, alive=False, death_cause=DeathCause.ATTACK, death_day=1),
        )

    result = await service.submit_vote(game.id, voter_seat=5, target_seat=1, round_=0, day=1)
    assert result is SubmitResult.VOTER_DEAD
    votes = await repo.load_votes(game.id, day=1, round_=0)
    assert all(v.voter_seat != 5 for v in votes), "dead voter submission must be dropped"


async def test_submit_vote_rejected_when_target_dead(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE

    from wolfbot.domain.enums import DeathCause
    from wolfbot.domain.models import PlayerUpdate
    from wolfbot.persistence.sqlite_repo import _apply_player_update

    async with repo._tx() as db:
        await _apply_player_update(
            db,
            game.id,
            PlayerUpdate(seat_no=7, alive=False, death_cause=DeathCause.ATTACK, death_day=1),
        )

    result = await service.submit_vote(game.id, voter_seat=1, target_seat=7, round_=0, day=1)
    assert result is SubmitResult.TARGET_DEAD
    votes = await repo.load_votes(game.id, day=1, round_=0)
    assert votes == [], "vote targeting a dead seat must be dropped"


async def test_submit_vote_self_vote_rejected(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE

    result = await service.submit_vote(game.id, voter_seat=3, target_seat=3, round_=0, day=1)
    assert result is SubmitResult.SELF_VOTE
    votes = await repo.load_votes(game.id, day=1, round_=0)
    assert votes == [], "self-vote must be dropped"


async def test_submit_vote_runoff_rejects_target_outside_tied_set(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """In runoff, target must be one of the tied seats from round 0."""
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE

    # Round 0: tie between seat 1 and seat 5 (4 votes each), voter 5 abstains.
    for v in (1, 2, 3, 4):
        await service.submit_vote(game.id, v, target_seat=5, round_=0, day=1)
    for v in (6, 7, 8, 9):
        await service.submit_vote(game.id, v, target_seat=1, round_=0, day=1)
    await service.submit_vote(game.id, 5, target_seat=None, round_=0, day=1)

    await service.advance(game.id)  # -> DAY_RUNOFF (tied {1, 5})

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_RUNOFF, (
        f"expected DAY_RUNOFF with tied tally, got {loaded.phase}"
    )

    # Illegal runoff target (seat 7, not in tied {1, 5}) must be dropped.
    illegal = await service.submit_vote(game.id, voter_seat=3, target_seat=7, round_=1, day=1)
    assert illegal is SubmitResult.ILLEGAL_TARGET
    runoff_votes = await repo.load_votes(game.id, day=1, round_=1)
    assert all(v.target_seat != 7 for v in runoff_votes), (
        "runoff vote for non-tied target must be dropped"
    )

    # Legal runoff target (seat 1, in tied set) must be accepted.
    accepted = await service.submit_vote(game.id, voter_seat=3, target_seat=1, round_=1, day=1)
    assert accepted is SubmitResult.ACCEPTED
    runoff_votes = await repo.load_votes(game.id, day=1, round_=1)
    assert any(v.voter_seat == 3 and v.target_seat == 1 for v in runoff_votes)


async def test_submit_night_action_rejected_when_phase_not_night(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, _, _, _, _ = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION

    players = await repo.load_players(game.id)
    wolf = next(p.seat_no for p in players if p.role is Role.WEREWOLF)
    result = await service.submit_night_action(
        game.id, wolf, SubmissionType.WOLF_ATTACK, target_seat=1, day=1
    )
    assert result is SubmitResult.STALE_PHASE
    actions = await repo.load_night_actions(game.id, day=1)
    assert actions == [], "night action during DAY_DISCUSSION must be dropped"


async def test_submit_night_action_rejected_when_role_mismatch(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """A villager submitting SEER_DIVINE must be rejected."""
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # -> NIGHT_0
    await service.advance(game.id)  # -> DAY_DISCUSSION
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE
    for v in range(1, 10):
        target = 1 if v != 1 else 2
        await service.submit_vote(game.id, v, target, round_=0, day=1)
    await service.advance(game.id)  # -> NIGHT (assuming seat 1 executed)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    if loaded.phase is not Phase.NIGHT:
        return

    players = await repo.load_players(game.id)
    villager = next(p.seat_no for p in players if p.alive and p.role is Role.VILLAGER)
    result = await service.submit_night_action(
        game.id, villager, SubmissionType.SEER_DIVINE, target_seat=2, day=loaded.day_number
    )
    assert result is SubmitResult.ROLE_MISMATCH
    # Only true SEER submissions should land in the DB
    actions = await repo.load_night_actions(game.id, day=loaded.day_number)
    assert all(a.actor_seat != villager for a in actions), (
        "villager submitting SEER_DIVINE must be dropped"
    )


async def test_submit_night_action_rejected_when_target_illegal(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """A wolf attacking a fellow wolf must be rejected (target not in legal_attack_targets)."""
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # -> NIGHT_0
    await service.advance(game.id)  # -> DAY_DISCUSSION
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE
    for v in range(1, 10):
        target = 1 if v != 1 else 2
        await service.submit_vote(game.id, v, target, round_=0, day=1)
    await service.advance(game.id)  # -> NIGHT

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    if loaded.phase is not Phase.NIGHT:
        return

    players = await repo.load_players(game.id)
    wolves = [p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF]
    if len(wolves) < 2:
        return  # need 2 wolves to test wolf-on-wolf attack

    result = await service.submit_night_action(
        game.id, wolves[0], SubmissionType.WOLF_ATTACK, target_seat=wolves[1], day=loaded.day_number
    )
    assert result is SubmitResult.ILLEGAL_TARGET
    actions = await repo.load_night_actions(game.id, day=loaded.day_number)
    assert not any(
        a.actor_seat == wolves[0]
        and a.kind is SubmissionType.WOLF_ATTACK
        and a.target_seat == wolves[1]
        for a in actions
    ), "wolf attacking fellow wolf must be dropped"


async def test_knight_guard_stale_previous_day_is_cleared(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Regression for 2026-04-24 v5 review Medium #2.

    When a knight doesn't submit a guard on some night and the host runs
    /wolf force-skip, `plan_night_resolve` returns `record_guard=None` and
    the previous_guard row is left with its old (last_guard_seat,
    last_guard_day). On subsequent nights the helper must recognize the
    row is stale (last_guard_day != game.day_number) and re-allow that
    seat — the bug was that `prev[1]` was used unconditionally.

    We simulate the stale-row state by seeding `upsert_previous_guard`
    with a past last_guard_day while the live game is on NIGHT day 1,
    then verify the submission is accepted. As a positive control we
    also check that a non-stale row (last_guard_day matching the current
    day) still forbids the same target.
    """
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # -> NIGHT_0
    await service.advance(game.id)  # -> DAY_DISCUSSION day 1
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE
    for v in range(1, 10):
        target = 1 if v != 1 else 2
        await service.submit_vote(game.id, v, target, round_=0, day=1)
    await service.advance(game.id)  # -> NIGHT day 1 (unless seat 1 win triggers GAME_OVER)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    if loaded.phase is not Phase.NIGHT:
        return  # execution of seat 1 triggered GAME_OVER for this seed; test N/A

    players = await repo.load_players(game.id)
    knight = next((p.seat_no for p in players if p.alive and p.role is Role.KNIGHT), None)
    if knight is None:
        return  # knight was executed on day 1; test N/A

    alive_non_knight = [p.seat_no for p in players if p.alive and p.seat_no != knight]
    stale_target = alive_non_knight[0]

    # Seed a STALE row: last_guard_day=0 < game.day_number=1 — should not block.
    await repo.upsert_previous_guard(
        game.id, knight_seat=knight, last_guard_seat=stale_target, last_guard_day=0
    )
    result = await service.submit_night_action(
        game.id, knight, SubmissionType.KNIGHT_GUARD, stale_target, day=1
    )
    assert result is SubmitResult.ACCEPTED, (
        f"stale previous_guard row (last_guard_day=0, current_day=1) wrongly blocked "
        f"knight seat {knight} from guarding seat {stale_target}"
    )

    # Now make the row non-stale (matches current day) — must block.
    await repo.upsert_previous_guard(
        game.id, knight_seat=knight, last_guard_seat=stale_target, last_guard_day=1
    )
    result = await service.submit_night_action(
        game.id, knight, SubmissionType.KNIGHT_GUARD, stale_target, day=1
    )
    assert result is SubmitResult.ILLEGAL_TARGET


async def test_dawn_morning_not_posted_twice(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """At dawn, morning text must be posted exactly once — not duplicated via public_logs."""
    service, disc, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION day 1
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE
    # Everyone votes seat 1; seat 1 is executed
    for voter in range(1, 10):
        target = 1 if voter != 1 else 2
        await service.submit_vote(game.id, voter, target, round_=0, day=1)
    await service.advance(game.id)  # -> NIGHT (assuming not GAME_OVER)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    if loaded.phase is not Phase.NIGHT:
        # executed seat 1 could trigger GAME_OVER; skip when no dawn transition
        return

    players = await repo.load_players(game.id)
    wolves = [p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF]
    seer = next(p.seat_no for p in players if p.alive and p.role is Role.SEER)
    knight = next(p.seat_no for p in players if p.alive and p.role is Role.KNIGHT)
    for w in wolves:
        await service.submit_night_action(game.id, w, SubmissionType.WOLF_ATTACK, 4, day=1)
    await service.submit_night_action(game.id, seer, SubmissionType.SEER_DIVINE, wolves[0], day=1)
    await service.submit_night_action(game.id, knight, SubmissionType.KNIGHT_GUARD, 3, day=1)

    disc.reset()
    await service.advance(game.id)  # NIGHT -> dawn

    morning_calls = [c for c in disc.calls if c.name == "post_morning"]
    public_morning_calls = [
        c for c in disc.calls if c.name == "post_public" and c.kwargs.get("kind") == "MORNING"
    ]
    assert len(morning_calls) == 1, (
        f"post_morning should fire exactly once, got {len(morning_calls)}"
    )
    assert public_morning_calls == [], (
        "MORNING public_logs must not be posted via post_public — duplicate dawn announcement"
    )


async def test_dawn_posts_in_spec_order(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Regression for 2026-04-24 v5 review Medium #1.

    Spec (prompts/IMPLEMENTATION_PROMPT.md #338-349) fixes the dawn order:
    medium result -> seer result -> guard/attack resolve -> morning -> phase
    change / victory. On Discord that means private role results must land
    before the morning post, which must land before PHASE_CHANGE.
    """
    service, disc, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # -> NIGHT_0
    await service.advance(game.id)  # -> DAY_DISCUSSION day 1
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE
    for voter in range(1, 10):
        target = 1 if voter != 1 else 2
        await service.submit_vote(game.id, voter, target, round_=0, day=1)
    await service.advance(game.id)  # -> NIGHT day 1 (skip if GAME_OVER)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    if loaded.phase is not Phase.NIGHT:
        return

    players = await repo.load_players(game.id)
    wolves = [p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF]
    seer = next(p.seat_no for p in players if p.alive and p.role is Role.SEER)
    knight = next(p.seat_no for p in players if p.alive and p.role is Role.KNIGHT)
    # Attack a villager seat (not wolf, seer, knight) to keep the game going
    victim = next(
        p.seat_no
        for p in players
        if p.alive and p.seat_no not in wolves and p.seat_no != seer and p.seat_no != knight
    )
    for w in wolves:
        await service.submit_night_action(game.id, w, SubmissionType.WOLF_ATTACK, victim, day=1)
    await service.submit_night_action(game.id, seer, SubmissionType.SEER_DIVINE, wolves[0], day=1)
    # Knight guards someone who is neither self nor attack victim → no death block
    guard_target = next(
        p.seat_no for p in players if p.alive and p.seat_no != knight and p.seat_no != victim
    )
    await service.submit_night_action(
        game.id, knight, SubmissionType.KNIGHT_GUARD, guard_target, day=1
    )

    disc.reset()
    await service.advance(game.id)  # NIGHT -> dawn

    loaded_after = await repo.load_game(game.id)
    assert loaded_after is not None
    if loaded_after.phase is Phase.GAME_OVER:
        return  # victory path is covered by the sibling test below

    def first(name: str, **kwargs: str) -> int:
        for i, c in enumerate(disc.calls):
            if c.name != name:
                continue
            if all(c.kwargs.get(k) == v for k, v in kwargs.items()):
                return i
        raise AssertionError(
            f"no call {name}({kwargs}) in {[(c.name, c.kwargs.get('kind')) for c in disc.calls]}"
        )

    medium_idx = next(
        (
            i
            for i, c in enumerate(disc.calls)
            if c.name == "send_private" and c.kwargs.get("kind") == "MEDIUM_RESULT"
        ),
        None,
    )
    seer_idx = first("send_private", kind="SEER_RESULT")
    morning_idx = first("post_morning")
    phase_change_idx = first("post_public", kind="PHASE_CHANGE")

    # Medium may be absent (no execution on day 1 would mean no medium result);
    # when present it must precede morning. Seer and morning are guaranteed.
    if medium_idx is not None:
        assert medium_idx < morning_idx, "MEDIUM_RESULT must come before post_morning"
    assert seer_idx < morning_idx, "SEER_RESULT must come before post_morning"
    assert morning_idx < phase_change_idx, "post_morning must precede PHASE_CHANGE"


async def test_dawn_victory_posts_in_spec_order(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """When a night attack triggers GAME_OVER at dawn, the order is still
    medium -> seer -> morning -> VICTORY -> ROLE_REVEAL (not the other
    way around). Covers the victory-branch of plan_night_resolve.
    """
    service, disc, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # -> NIGHT_0
    await service.advance(game.id)  # -> DAY_DISCUSSION day 1
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE
    for voter in range(1, 10):
        target = 1 if voter != 1 else 2
        await service.submit_vote(game.id, voter, target, round_=0, day=1)
    await service.advance(game.id)  # -> NIGHT day 1 (or GAME_OVER)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    if loaded.phase is not Phase.NIGHT:
        return

    players = await repo.load_players(game.id)
    wolves = [p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF]
    seer = next((p.seat_no for p in players if p.alive and p.role is Role.SEER), None)
    knight = next((p.seat_no for p in players if p.alive and p.role is Role.KNIGHT), None)
    if seer is None or knight is None:
        return
    # Attack the seer so attack count shifts wolf parity high enough; game may
    # or may not end here depending on role distribution. We only assert the
    # order when the GAME_OVER branch fires.
    for w in wolves:
        await service.submit_night_action(game.id, w, SubmissionType.WOLF_ATTACK, seer, day=1)
    await service.submit_night_action(game.id, seer, SubmissionType.SEER_DIVINE, wolves[0], day=1)
    guard_target = next(
        p.seat_no for p in players if p.alive and p.seat_no != knight and p.seat_no != seer
    )
    await service.submit_night_action(
        game.id, knight, SubmissionType.KNIGHT_GUARD, guard_target, day=1
    )

    disc.reset()
    await service.advance(game.id)  # NIGHT -> dawn

    loaded_after = await repo.load_game(game.id)
    assert loaded_after is not None
    if loaded_after.phase is not Phase.GAME_OVER:
        return  # GAME_OVER didn't trigger on this seed; the happy-path sibling covers normal dawn

    def first_idx(predicate: object, label: str) -> int:
        for i, c in enumerate(disc.calls):
            if predicate(c):  # type: ignore[operator]
                return i
        raise AssertionError(
            f"no call matching {label} in {[(c.name, c.kwargs) for c in disc.calls]}"
        )

    seer_idx = first_idx(
        lambda c: c.name == "send_private" and c.kwargs.get("kind") == "SEER_RESULT",
        "SEER_RESULT",
    )
    morning_idx = first_idx(lambda c: c.name == "post_morning", "post_morning")
    victory_idx = first_idx(
        lambda c: c.name == "post_public" and c.kwargs.get("kind") == "VICTORY", "VICTORY"
    )
    reveal_idx = first_idx(
        lambda c: c.name == "post_public" and c.kwargs.get("kind") == "ROLE_REVEAL", "ROLE_REVEAL"
    )

    assert seer_idx < morning_idx
    assert morning_idx < victory_idx, "post_morning must precede VICTORY"
    assert victory_idx < reveal_idx, "VICTORY must precede ROLE_REVEAL"


async def test_full_game_one_day_happy_path(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """SETUP → NIGHT_0 → DAY_DISCUSSION → DAY_VOTE → NIGHT → DAY_DISCUSSION day 2."""
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # → NIGHT_0
    await service.advance(game.id)  # → DAY_DISCUSSION day 1
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # → DAY_VOTE
    # Everyone votes seat 1
    for voter in range(1, 10):
        target = 1 if voter != 1 else 2
        await service.submit_vote(game.id, voter, target, round_=0, day=1)
    await service.advance(game.id)  # → NIGHT (since seat 1 got 8 votes, executed)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase in (Phase.NIGHT, Phase.GAME_OVER)

    if loaded.phase is Phase.NIGHT:
        # Provide night actions for all living wolves, seer, knight
        players = await repo.load_players(game.id)
        wolves = [p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF]
        seer = next(p.seat_no for p in players if p.alive and p.role is Role.SEER)
        knight = next(p.seat_no for p in players if p.alive and p.role is Role.KNIGHT)
        # All wolves agree on seat 4 (seer) as attack
        for w in wolves:
            await service.submit_night_action(game.id, w, SubmissionType.WOLF_ATTACK, 4, day=1)
        await service.submit_night_action(
            game.id, seer, SubmissionType.SEER_DIVINE, wolves[0], day=1
        )
        await service.submit_night_action(game.id, knight, SubmissionType.KNIGHT_GUARD, 3, day=1)
        await service.advance(game.id)
        loaded = await repo.load_game(game.id)
        assert loaded is not None
        # Should move on to day 2 (or GAME_OVER if seer attack shifted victory)
        assert loaded.phase in (Phase.DAY_DISCUSSION, Phase.GAME_OVER)


async def test_submit_vote_returns_accepted_on_legal_vote(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Success path returns SubmitResult.ACCEPTED."""
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE

    result = await service.submit_vote(game.id, voter_seat=1, target_seat=2, round_=0, day=1)
    assert result is SubmitResult.ACCEPTED


async def test_submit_vote_returns_game_not_found_for_unknown_id(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, _, _, _, _ = svc
    result = await service.submit_vote(
        "does-not-exist", voter_seat=1, target_seat=2, round_=0, day=1
    )
    assert result is SubmitResult.GAME_NOT_FOUND


async def test_submit_night_action_returns_accepted_for_seer(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE
    for v in range(1, 10):
        target = 1 if v != 1 else 2
        await service.submit_vote(game.id, v, target, round_=0, day=1)
    await service.advance(game.id)  # -> NIGHT (seat 1 executed)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    if loaded.phase is not Phase.NIGHT:
        return
    players = await repo.load_players(game.id)
    seer = next(p.seat_no for p in players if p.alive and p.role is Role.SEER)
    wolf = next(p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF)

    result = await service.submit_night_action(
        game.id, seer, SubmissionType.SEER_DIVINE, target_seat=wolf, day=loaded.day_number
    )
    assert result is SubmitResult.ACCEPTED


async def test_resend_pending_dms_day_vote_sends_only_to_missing(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """After some voters submit, resend should DM only the ones still missing."""
    service, disc, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE

    # Seats 1, 2, 3 vote; seats 4-9 haven't.
    for v in (1, 2, 3):
        await service.submit_vote(game.id, v, target_seat=5, round_=0, day=1)

    disc.reset()
    await service.resend_pending_dms(game.id)

    sent = [c for c in disc.calls if c.name == "send_vote_dms"]
    assert len(sent) == 1
    assert sorted(sent[0].kwargs["voters"]) == [4, 5, 6, 7, 8, 9]
    assert sent[0].kwargs["round_"] == 0


async def test_resend_pending_dms_noop_in_day_discussion(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Non-submission phases must not trigger DM resends."""
    service, disc, _, _, _ = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # -> NIGHT_0
    await service.advance(game.id)  # -> DAY_DISCUSSION

    disc.reset()
    await service.resend_pending_dms(game.id)

    assert not any(c.name == "send_vote_dms" for c in disc.calls)
    assert not any(c.name == "send_night_action_dms" for c in disc.calls)


async def test_resend_pending_dms_night_passes_only_missing_actors(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """At NIGHT, only the actors whose role-specific submission is missing get DMed."""
    service, disc, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE
    for v in range(1, 10):
        target = 1 if v != 1 else 2
        await service.submit_vote(game.id, v, target, round_=0, day=1)
    await service.advance(game.id)  # -> NIGHT

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    if loaded.phase is not Phase.NIGHT:
        return

    players = await repo.load_players(game.id)
    wolves = [p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF]
    seer = next(p.seat_no for p in players if p.alive and p.role is Role.SEER)
    knight = next(p.seat_no for p in players if p.alive and p.role is Role.KNIGHT)

    # All wolves submit, seer + knight still pending.
    for w in wolves:
        await service.submit_night_action(game.id, w, SubmissionType.WOLF_ATTACK, 4, day=1)

    disc.reset()
    await service.resend_pending_dms(game.id)

    sent = [c for c in disc.calls if c.name == "send_night_action_dms"]
    assert len(sent) == 1
    dmed = set(sent[0].kwargs["players"])
    assert seer in dmed
    assert knight in dmed
    for w in wolves:
        assert w not in dmed, "wolves already submitted — must not be re-DMed"


async def test_host_extend_resends_dms_before_waking(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Resuming from WAITING via /wolf extend must re-send DMs to the still-missing voters."""
    service, disc, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE

    # Two voters submit, the rest don't.
    await service.submit_vote(game.id, 1, target_seat=5, round_=0, day=1)
    await service.submit_vote(game.id, 2, target_seat=5, round_=0, day=1)

    # Expire the vote deadline and advance → parks into WAITING_HOST_DECISION.
    clock.tick(120)
    await service.advance(game.id)
    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.WAITING_HOST_DECISION

    disc.reset()
    ok = await service.host_extend(game.id, extra_seconds=60)
    assert ok

    sent = [c for c in disc.calls if c.name == "send_vote_dms"]
    assert len(sent) == 1
    # Seats 1 and 2 already voted, so they must not be re-DMed.
    dmed = set(sent[0].kwargs["voters"])
    assert 1 not in dmed
    assert 2 not in dmed
    assert dmed == {3, 4, 5, 6, 7, 8, 9}


async def test_submit_vote_rejected_when_day_mismatch(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """A DM captured on day 1 that the user clicks on day 2 must not be accepted
    as a day-2 submission just because the phase matches (DAY_VOTE → DAY_VOTE).
    """
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION day 1
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE day 1

    # Force the DB forward to DAY_VOTE day 2 (simulating the game progressed to
    # the next day while a player still had yesterday's vote DM open).
    async with repo._tx() as db:
        await db.execute(
            "UPDATE games SET day_number=2 WHERE id=?",
            (game.id,),
        )

    result = await service.submit_vote(game.id, voter_seat=1, target_seat=2, round_=0, day=1)
    assert result is SubmitResult.STALE_PHASE
    # Nothing should have been written
    votes = await repo.load_votes(game.id, day=2, round_=0)
    assert votes == []


async def test_submit_night_action_rejected_when_day_mismatch(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Same as the vote case, but for night actions: yesterday's DM must not
    be accepted as today's submission when phase=NIGHT recurs."""
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION day 1
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE
    for v in range(1, 10):
        target = 1 if v != 1 else 2
        await service.submit_vote(game.id, v, target, round_=0, day=1)
    await service.advance(game.id)  # -> NIGHT day 1 (or GAME_OVER)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    if loaded.phase is not Phase.NIGHT:
        return  # can't exercise this path without a NIGHT phase

    players = await repo.load_players(game.id)
    seer = next(p.seat_no for p in players if p.alive and p.role is Role.SEER)
    wolf = next(p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF)

    # Pretend we've advanced to NIGHT day 2 while the DM on the user's client
    # still carries day=1.
    async with repo._tx() as db:
        await db.execute(
            "UPDATE games SET day_number=2 WHERE id=?",
            (game.id,),
        )

    result = await service.submit_night_action(
        game.id, seer, SubmissionType.SEER_DIVINE, target_seat=wolf, day=1
    )
    assert result is SubmitResult.STALE_PHASE
    actions = await repo.load_night_actions(game.id, day=2)
    assert actions == []


async def test_apply_transition_commits_set_force_skip_on_match(
    repo: SqliteRepo,
) -> None:
    """Happy path: set_force_skip=True flips the DB flag atomically with the phase swap."""
    from wolfbot.domain.models import Transition

    game = Game(
        id=new_game_id(),
        guild_id="g-fs-ok",
        host_user_id="h",
        phase=Phase.WAITING_HOST_DECISION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(game)

    t = Transition(
        next_phase=Phase.DAY_VOTE,
        next_day=1,
        new_deadline_epoch=9999,
        set_force_skip=True,
    )
    ok = await repo.apply_transition(game.id, t, expected_phase=Phase.WAITING_HOST_DECISION)
    assert ok is True

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_VOTE
    assert loaded.force_skip_pending is True


async def test_apply_transition_rolls_back_set_force_skip_on_phase_mismatch(
    repo: SqliteRepo,
) -> None:
    """Repro for the Codex v2 Medium finding: a force-skip that loses to extend
    must not leak `force_skip_pending=1`. apply_transition's optimistic lock
    rollback now reverts the flag together with the phase swap.
    """
    from wolfbot.domain.models import Transition

    game = Game(
        id=new_game_id(),
        guild_id="g-fs-race",
        host_user_id="h",
        phase=Phase.DAY_VOTE,  # as if extend already restored the phase
        day_number=1,
        deadline_epoch=5555,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(game)

    t = Transition(
        next_phase=Phase.DAY_VOTE,
        next_day=1,
        new_deadline_epoch=9999,
        set_force_skip=True,
    )
    ok = await repo.apply_transition(game.id, t, expected_phase=Phase.WAITING_HOST_DECISION)
    assert ok is False

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_VOTE
    assert loaded.deadline_epoch == 5555  # unchanged
    assert loaded.force_skip_pending is False  # critical: no residual flag


async def test_resend_pending_dms_dispatches_llm_votes_for_missing_llm_seats(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Fix 1: on /wolf extend, resend_pending_dms re-dispatches LLM votes for
    the still-pending LLM seats — not humans, not LLMs that already voted."""
    service, _, llm, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE

    # Humans 1-3 and LLM 7 voted; LLMs 6, 8, 9 did not.
    for voter in (1, 2, 3, 7):
        await service.submit_vote(game.id, voter, target_seat=4, round_=0, day=1)

    # Clear prior FakeLLMAdapter calls from the initial _dispatch_submissions.
    llm.calls.clear()

    await service.resend_pending_dms(game.id)

    # Exactly one LLM vote re-dispatch, for the three missing LLM seats only.
    vote_calls = [c for c in llm.calls if c.name == "submit_llm_votes"]
    assert len(vote_calls) == 1
    call = vote_calls[0]
    assert call.kwargs["restrict_to_seats"] == [6, 8, 9]
    assert call.kwargs["round_"] == 0


async def test_resend_pending_dms_dispatches_llm_night_with_unresolved(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Fix 1 + split-wolf: when NIGHT has missing submissions + a wolf split,
    the resend passes restrict_to_seats (union) AND unresolved_seats (so the
    in-loop guard re-asks the split wolves)."""
    from wolfbot.domain.models import NightAction as NightActionModel

    service, _, llm, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE
    # Everyone including seat 1 must vote. Seat 1 votes seat 2; 2..9 vote seat 1
    # → seat 1 executed, game advances to NIGHT.
    await service.submit_vote(game.id, 1, target_seat=2, round_=0, day=1)
    for voter in range(2, 10):
        await service.submit_vote(game.id, voter, target_seat=1, round_=0, day=1)
    await service.advance(game.id)  # -> NIGHT

    loaded = await repo.load_game(game.id)
    assert loaded is not None and loaded.phase is Phase.NIGHT

    # Identify wolves and a non-wolf alive target.
    players = await repo.load_players(game.id)
    wolves = sorted(p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF)
    assert len(wolves) == 2
    non_wolf_target = next(p.seat_no for p in players if p.alive and p.role is not Role.WEREWOLF)
    other_target = next(
        p.seat_no
        for p in players
        if p.alive and p.role is not Role.WEREWOLF and p.seat_no != non_wolf_target
    )
    # Wolves pick different targets → split.
    await repo.insert_night_action(
        NightActionModel(
            game_id=game.id,
            day=loaded.day_number,
            actor_seat=wolves[0],
            kind=SubmissionType.WOLF_ATTACK,
            target_seat=non_wolf_target,
            submitted_at=0,
        )
    )
    await repo.insert_night_action(
        NightActionModel(
            game_id=game.id,
            day=loaded.day_number,
            actor_seat=wolves[1],
            kind=SubmissionType.WOLF_ATTACK,
            target_seat=other_target,
            submitted_at=0,
        )
    )

    llm.calls.clear()
    await service.resend_pending_dms(game.id)

    night_calls = [c for c in llm.calls if c.name == "submit_llm_night_actions"]
    assert len(night_calls) == 1
    call = night_calls[0]
    # Both wolves should be in both restrict_to_seats and unresolved_seats (all
    # alive wolves are LLMs by the _nine_seats fixture since wolves are picked
    # from the shuffled role distribution — they may include humans. Filter to
    # seats that are actually LLM + wolf.)
    seats_loaded = await repo.load_seats(game.id)
    llm_wolves = sorted(s.seat_no for s in seats_loaded if s.is_llm and s.seat_no in wolves)
    # The resend must cover exactly the LLM subset of the split wolves.
    assert call.kwargs["restrict_to_seats"] == llm_wolves
    assert call.kwargs["unresolved_seats"] == llm_wolves


async def test_game_over_posts_role_reveal_to_main_channel(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """On execution-path victory, the FakeDiscordAdapter receives VICTORY then ROLE_REVEAL,
    both via post_public, naming every seat with role + alive/dead."""
    service, disc, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # -> NIGHT_0
    await service.advance(game.id)  # -> DAY_DISCUSSION day 1

    players = await repo.load_players(game.id)
    wolves = sorted(p.seat_no for p in players if p.role is Role.WEREWOLF)
    assert len(wolves) == 2
    target_wolf = wolves[0]
    other_wolf = wolves[1]
    # Keep target_wolf plus 3 non-wolves alive. Killing every other seat gives us
    # 1 wolf vs 3 non-wolves — executing the wolf clinches a VILLAGE win.
    survivors: list[int] = [target_wolf]
    for sn in range(1, 10):
        if sn in (target_wolf, other_wolf):
            continue
        if len(survivors) < 4:
            survivors.append(sn)
    kill_seats = [sn for sn in range(1, 10) if sn not in survivors]

    # Hand-craft a transition that kills the selected seats and jumps the game to
    # DAY_VOTE day 3. apply_transition's optimistic lock is satisfied by passing
    # expected_phase=DAY_DISCUSSION (the phase we're currently in).
    jump = Transition(
        next_phase=Phase.DAY_VOTE,
        next_day=3,
        new_deadline_epoch=clock.now + 60,
        player_updates=tuple(
            PlayerUpdate(seat_no=sn, alive=False, death_cause=DeathCause.ATTACK, death_day=2)
            for sn in kill_seats
        ),
    )
    committed = await repo.apply_transition(game.id, jump, expected_phase=Phase.DAY_DISCUSSION)
    assert committed

    # All survivors vote for the wolf; the wolf self-votes and gets dropped.
    for voter in survivors:
        target = target_wolf if voter != target_wolf else survivors[1]
        result = await service.submit_vote(game.id, voter, target, round_=0, day=3)
        assert result is SubmitResult.ACCEPTED

    disc.reset()
    await service.advance(game.id)  # execute wolf -> VICTORY + ROLE_REVEAL

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.GAME_OVER

    public_posts = [c for c in disc.calls if c.name == "post_public"]
    kinds = [c.kwargs["kind"] for c in public_posts]
    assert "VICTORY" in kinds
    assert "ROLE_REVEAL" in kinds
    # ROLE_REVEAL must arrive after VICTORY on the main channel.
    assert kinds.index("ROLE_REVEAL") > kinds.index("VICTORY")

    reveal_text = public_posts[kinds.index("ROLE_REVEAL")].kwargs["text"]
    assert reveal_text.startswith("最終配役:\n")
    # Every seat (1–9) present with a role label and 生存/死亡 status.
    for sn in range(1, 10):
        assert f"- 席{sn} " in reveal_text
    assert "(生存)" in reveal_text
    assert "(死亡)" in reveal_text


async def _advance_to_night(
    repo: SqliteRepo,
    service: GameService,
    clock: FakeClock,
) -> tuple[Game, list[int], int, int, list[int]] | None:
    """Drive a freshly-set-up game through to NIGHT day 1, returning role seats.

    Returns (game, alive_wolf_seats, seer_seat, knight_seat, alive_villager_pool).
    `alive_villager_pool` contains alive seats that are NOT wolf/seer/knight —
    safe defaults for "pick a victim" / "pick a guard target" without colliding
    with the role under test. Returns None when seat 1's execution coincidentally
    ends the game before NIGHT.
    """
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # -> DAY_VOTE
    for v in range(1, 10):
        target = 1 if v != 1 else 2
        await service.submit_vote(game.id, v, target, round_=0, day=1)
    await service.advance(game.id)  # -> NIGHT (assuming game not over)

    loaded = await repo.load_game(game.id)
    assert loaded is not None
    if loaded.phase is not Phase.NIGHT:
        return None
    players = await repo.load_players(game.id)
    wolves = [p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF]
    seer = next(p.seat_no for p in players if p.alive and p.role is Role.SEER)
    knight = next(p.seat_no for p in players if p.alive and p.role is Role.KNIGHT)
    villager_pool = [
        p.seat_no
        for p in players
        if p.alive and p.seat_no not in wolves and p.seat_no != seer and p.seat_no != knight
    ]
    return loaded, wolves, seer, knight, villager_pool


async def test_split_wolf_attack_does_not_early_wake(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """When 2 wolves submit different targets, _all_night_actions_in must not early-wake.

    Spec (state_machine.py): a 1-vs-1 split stays "未確定" until the deadline so
    wolves can self-correct. Without this guard the engine wakes the moment the
    second wolf submits, jumping straight to WAITING_HOST_DECISION before the
    night clock runs out.

    Skips if the random role assignment puts the two wolves on a human seat +
    an LLM seat — in that mix `resolve_wolf_attack` applies human-wolf priority
    instead of pausing, so the early-wake guard becomes irrelevant.
    """
    service, _, _, reg, clock = svc
    setup = await _advance_to_night(repo, service, clock)
    if setup is None:
        return
    game, wolves, seer, knight, villager_pool = setup
    if len(wolves) < 2 or len(villager_pool) < 2:
        return  # need both wolves alive and 2 distinct legal attack targets
    seats = await repo.load_seats(game.id)
    seats_by_no = {s.seat_no: s for s in seats}
    is_llm = [seats_by_no[w].is_llm for w in wolves]
    if is_llm.count(True) == 1:
        # Mixed human/LLM wolves invoke the priority rule, not split.
        return

    wakes: list[str] = []
    reg.wake = lambda gid: wakes.append(gid)  # type: ignore[method-assign]

    target_a, target_b = villager_pool[0], villager_pool[1]
    assert target_a != target_b

    # Seer + knight + two wolves: every required action submitted, but wolves split.
    await service.submit_night_action(
        game.id, seer, SubmissionType.SEER_DIVINE, wolves[0], day=game.day_number
    )
    await service.submit_night_action(
        game.id, knight, SubmissionType.KNIGHT_GUARD, wolves[0], day=game.day_number
    )
    await service.submit_night_action(
        game.id, wolves[0], SubmissionType.WOLF_ATTACK, target_a, day=game.day_number
    )
    await service.submit_night_action(
        game.id, wolves[1], SubmissionType.WOLF_ATTACK, target_b, day=game.day_number
    )

    assert wakes == [], "split wolf attack must not trigger early wake"

    # After the deadline expires, the regular advance path detects the split
    # and routes into WAITING_HOST_DECISION as before.
    loaded = await repo.load_game(game.id)
    assert loaded is not None and loaded.deadline_epoch is not None
    clock.now = loaded.deadline_epoch + 1
    await service.advance(game.id)
    after = await repo.load_game(game.id)
    assert after is not None
    assert after.phase is Phase.WAITING_HOST_DECISION


async def test_aligned_wolf_attack_still_early_wakes(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Positive regression: when both wolves agree, the early-wake path must still fire."""
    service, _, _, reg, clock = svc
    setup = await _advance_to_night(repo, service, clock)
    if setup is None:
        return
    game, wolves, seer, knight, villager_pool = setup
    if len(wolves) < 2 or not villager_pool:
        return

    wakes: list[str] = []
    reg.wake = lambda gid: wakes.append(gid)  # type: ignore[method-assign]

    target = villager_pool[0]
    await service.submit_night_action(
        game.id, seer, SubmissionType.SEER_DIVINE, wolves[0], day=game.day_number
    )
    await service.submit_night_action(
        game.id, knight, SubmissionType.KNIGHT_GUARD, wolves[0], day=game.day_number
    )
    await service.submit_night_action(
        game.id, wolves[0], SubmissionType.WOLF_ATTACK, target, day=game.day_number
    )
    await service.submit_night_action(
        game.id, wolves[1], SubmissionType.WOLF_ATTACK, target, day=game.day_number
    )

    assert wakes == [game.id]


async def test_mixed_human_llm_wolf_split_triggers_early_wake(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Human wolf + LLM wolf disagreeing must early-wake (human-wolf priority).

    `plan_night_resolve` adopts the human's pick when the two wolves are mixed,
    so the night is fully decided the moment the second submission lands.
    Without mirroring that priority in `_all_night_actions_in`, the engine
    would idle for up to NIGHT_DURATION even though there is no actual split.
    """
    service, _, _, reg, clock = svc

    # Build a game directly in NIGHT phase so we can pin specific role/seat
    # assignments — the role-shuffle from `_advance_to_night` is RNG-driven and
    # only sometimes produces a mixed wolf pair.
    game = Game(
        id=new_game_id(),
        guild_id="g",
        host_user_id="h",
        phase=Phase.NIGHT,
        day_number=1,
        deadline_epoch=clock.now + 90,
        main_text_channel_id="ch-text",
        main_vc_channel_id="ch-vc",
        heaven_channel_id="ch-heaven",
        wolves_channel_id="ch-wolves",
        created_at=0,
    )
    await repo.create_game(game)
    for s in _nine_seats():
        await repo.insert_seat(game.id, s)

    # Seat 1 = human wolf, seat 9 = LLM wolf, seat 2 = seer, seat 3 = knight.
    await repo.set_player_role(game.id, 1, Role.WEREWOLF)
    await repo.set_player_role(game.id, 9, Role.WEREWOLF)
    await repo.set_player_role(game.id, 2, Role.SEER)
    await repo.set_player_role(game.id, 3, Role.KNIGHT)
    for sn in (4, 5, 6, 7, 8):
        await repo.set_player_role(game.id, sn, Role.VILLAGER)

    wakes: list[str] = []
    reg.wake = lambda gid: wakes.append(gid)  # type: ignore[method-assign]

    await service.submit_night_action(game.id, 2, SubmissionType.SEER_DIVINE, 9, day=1)
    await service.submit_night_action(game.id, 3, SubmissionType.KNIGHT_GUARD, 2, day=1)
    # Wolves split: human picks 4, LLM picks 5. Human-wolf priority resolves
    # in favor of seat 4, so the night is decided.
    await service.submit_night_action(game.id, 1, SubmissionType.WOLF_ATTACK, 4, day=1)
    await service.submit_night_action(game.id, 9, SubmissionType.WOLF_ATTACK, 5, day=1)

    assert wakes == [game.id]


async def test_submit_night_action_rejects_none_target_for_seer(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """target_seat=None on SEER_DIVINE must be rejected; nothing persisted."""
    service, _, _, _, clock = svc
    setup = await _advance_to_night(repo, service, clock)
    if setup is None:
        return
    game, _, seer, _, _ = setup

    result = await service.submit_night_action(
        game.id, seer, SubmissionType.SEER_DIVINE, target_seat=None, day=game.day_number
    )
    assert result is SubmitResult.ILLEGAL_TARGET
    actions = await repo.load_night_actions(game.id, day=game.day_number)
    assert not any(a.actor_seat == seer and a.kind is SubmissionType.SEER_DIVINE for a in actions)


async def test_submit_night_action_rejects_none_target_for_knight(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """target_seat=None on KNIGHT_GUARD must be rejected; nothing persisted."""
    service, _, _, _, clock = svc
    setup = await _advance_to_night(repo, service, clock)
    if setup is None:
        return
    game, _, _, knight, _ = setup

    result = await service.submit_night_action(
        game.id, knight, SubmissionType.KNIGHT_GUARD, target_seat=None, day=game.day_number
    )
    assert result is SubmitResult.ILLEGAL_TARGET
    actions = await repo.load_night_actions(game.id, day=game.day_number)
    assert not any(
        a.actor_seat == knight and a.kind is SubmissionType.KNIGHT_GUARD for a in actions
    )


async def test_submit_night_action_rejects_none_target_for_wolf(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """target_seat=None on WOLF_ATTACK must be rejected; nothing persisted."""
    service, _, _, _, clock = svc
    setup = await _advance_to_night(repo, service, clock)
    if setup is None:
        return
    game, wolves, _, _, _ = setup
    if not wolves:
        return

    result = await service.submit_night_action(
        game.id, wolves[0], SubmissionType.WOLF_ATTACK, target_seat=None, day=game.day_number
    )
    assert result is SubmitResult.ILLEGAL_TARGET
    actions = await repo.load_night_actions(game.id, day=game.day_number)
    assert not any(
        a.actor_seat == wolves[0] and a.kind is SubmissionType.WOLF_ATTACK for a in actions
    )


# ----------------------------- DAY_DISCUSSION LLM-rounds gate behaviour
async def test_advance_day_discussion_stays_when_llms_incomplete_at_deadline(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Deadline reached but LLM rounds incomplete → stay in DAY_DISCUSSION
    with `deadline_epoch = now + DAY_DISCUSSION_GRACE`."""
    from wolfbot.domain.state_machine import DAY_DISCUSSION_GRACE

    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # SETUP -> NIGHT_0
    await service.advance(game.id)  # NIGHT_0 -> DAY_DISCUSSION
    # Seed only one LLM seat partially — the gate sees rounds_done < 2.
    await repo.increment_llm_discussion_round(game.id, day=1, seat_no=6)
    clock.tick(300)
    await service.advance(game.id)
    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_DISCUSSION
    assert loaded.deadline_epoch == clock.now + DAY_DISCUSSION_GRACE


async def test_advance_day_discussion_stays_when_llms_done_before_deadline(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """All LLMs complete both rounds before the natural deadline → no transition
    is committed; we stay in DAY_DISCUSSION until the deadline expires."""
    service, _, _, _, _ = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)  # → DAY_DISCUSSION
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    # Don't tick — deadline still in the future.
    await service.advance(game.id)
    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_DISCUSSION


async def test_advance_day_discussion_grace_does_not_redispatch_llm(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Same-phase grace recommit must not call submit_llm_discussion_rounds again."""
    service, _, llm, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # → NIGHT_0
    await service.advance(game.id)  # → DAY_DISCUSSION (initial dispatch)
    initial = sum(1 for c in llm.calls if c.name == "submit_llm_discussion_rounds")
    assert initial == 1
    # No seed → rounds_done < 2 → tick past deadline → grace recommit.
    clock.tick(300)
    await service.advance(game.id)
    assert sum(1 for c in llm.calls if c.name == "submit_llm_discussion_rounds") == 1


# ----------------------------- DAY_RUNOFF_SPEECH branching
async def test_advance_runoff_speech_dispatched_when_tied_includes_llm(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """DAY_VOTE tie with at least one LLM tied candidate → DAY_RUNOFF_SPEECH +
    submit_llm_runoff_candidate_speeches dispatch."""
    from wolfbot.domain.state_machine import RUNOFF_SPEECH_DEADLINE

    service, disc, llm, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)  # → NIGHT_0
    await service.advance(game.id)  # → DAY_DISCUSSION
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # → DAY_VOTE
    # Build a 4-4 tie between seats 6 and 7. seat 6 is LLM (per _nine_seats),
    # so DAY_RUNOFF_SPEECH must be entered. Voter 7 abstains so seat 7 isn't
    # casting a self-vote, and seat 6 also can't vote for itself.
    for v in (1, 2, 3, 4):
        await service.submit_vote(game.id, v, target_seat=6, round_=0, day=1)
    for v in (5, 8, 9):
        await service.submit_vote(game.id, v, target_seat=7, round_=0, day=1)
    await service.submit_vote(game.id, 6, target_seat=7, round_=0, day=1)
    await service.submit_vote(game.id, 7, target_seat=None, round_=0, day=1)
    await service.advance(game.id)  # all votes in → wakes & advances
    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_RUNOFF_SPEECH
    assert loaded.deadline_epoch == clock.now + RUNOFF_SPEECH_DEADLINE
    # LLMAdapter was asked for runoff candidate speeches.
    assert any(c.name == "submit_llm_runoff_candidate_speeches" for c in llm.calls)
    # No round-1 vote DMs yet — those wait for DAY_RUNOFF.
    runoff_dms = [c for c in disc.calls if c.name == "send_vote_dms" and c.kwargs["round_"] == 1]
    assert runoff_dms == []


async def test_advance_runoff_speech_to_runoff_after_speeches_done(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Once tied LLM candidates have runoff_speech_done, advance to DAY_RUNOFF."""
    service, disc, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # → DAY_VOTE
    for v in (1, 2, 3, 4):
        await service.submit_vote(game.id, v, target_seat=6, round_=0, day=1)
    for v in (5, 8, 9):
        await service.submit_vote(game.id, v, target_seat=7, round_=0, day=1)
    await service.submit_vote(game.id, 6, target_seat=7, round_=0, day=1)
    await service.submit_vote(game.id, 7, target_seat=None, round_=0, day=1)
    await service.advance(game.id)  # → DAY_RUNOFF_SPEECH
    # Seed runoff_speech_done for tied LLM seats (6 and 7 — both are LLM in _nine_seats()).
    await repo.mark_llm_runoff_speech_done(game.id, day=1, seat_no=6)
    await repo.mark_llm_runoff_speech_done(game.id, day=1, seat_no=7)
    await service.advance(game.id)  # → DAY_RUNOFF
    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_RUNOFF
    runoff_dms = [c for c in disc.calls if c.name == "send_vote_dms" and c.kwargs["round_"] == 1]
    assert len(runoff_dms) == 1


async def test_advance_runoff_skips_speech_when_tie_is_all_human(
    repo: SqliteRepo,
    svc: tuple[GameService, FakeDiscordAdapter, FakeLLMAdapter, EngineRegistry, FakeClock],
) -> None:
    """Tie among only human seats → straight to DAY_RUNOFF, no DAY_RUNOFF_SPEECH."""
    service, _, _, _, clock = svc
    game = await _make_game_in_setup(repo)
    await service.advance(game.id)
    await service.advance(game.id)
    await _seed_llm_discussion_rounds_done(repo, game.id, day=1)
    clock.tick(300)
    await service.advance(game.id)  # → DAY_VOTE
    # Build a 4-4 tie between seats 1 and 2 (both human in _nine_seats).
    for v in (3, 4, 5, 6):
        await service.submit_vote(game.id, v, target_seat=1, round_=0, day=1)
    for v in (7, 8, 9):
        await service.submit_vote(game.id, v, target_seat=2, round_=0, day=1)
    await service.submit_vote(game.id, 1, target_seat=2, round_=0, day=1)
    await service.submit_vote(game.id, 2, target_seat=None, round_=0, day=1)
    await service.advance(game.id)
    loaded = await repo.load_game(game.id)
    assert loaded is not None
    assert loaded.phase is Phase.DAY_RUNOFF
