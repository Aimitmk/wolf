"""GameEngine timer behavior: transient phase auto-advance, deadline wait, wake."""

from __future__ import annotations

import asyncio

import pytest

from tests.fakes import FakeClock
from wolfbot.domain.enums import Phase
from wolfbot.domain.models import Game
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.game_service import new_game_id
from wolfbot.services.timer_service import EngineRegistry, GameEngine


async def _make_game(repo: SqliteRepo, phase: Phase, deadline: int | None) -> Game:
    g = Game(
        id=new_game_id(),
        guild_id="g",
        host_user_id="h",
        phase=phase,
        day_number=1,
        deadline_epoch=deadline,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(g)
    return g


async def test_engine_advances_transient_phase_immediately(repo: SqliteRepo) -> None:
    """Phases with no deadline should cause the engine to call advance without waiting."""
    from wolfbot.domain.models import Transition

    clock = FakeClock(now=0)
    advance_count = 0

    game = await _make_game(repo, phase=Phase.SETUP, deadline=None)

    async def fake_advance(game_id: str) -> None:
        nonlocal advance_count
        advance_count += 1
        await repo.apply_transition(
            game_id,
            Transition(next_phase=Phase.GAME_OVER, next_day=1, new_deadline_epoch=None),
            expected_phase=Phase.SETUP,
        )

    engine = GameEngine(game_id=game.id, repo=repo, advance=fake_advance, clock=clock)
    engine.start()
    assert engine._task is not None  # type: ignore[attr-defined]
    try:
        await asyncio.wait_for(engine._task, timeout=2)  # type: ignore[attr-defined]
    except TimeoutError:
        pytest.fail("engine did not finish in time")
    assert advance_count == 1


async def test_engine_parks_on_waiting_host_decision(repo: SqliteRepo) -> None:
    game = await _make_game(repo, phase=Phase.WAITING_HOST_DECISION, deadline=None)
    advance_called: list[str] = []

    async def fake_advance(game_id: str) -> None:
        advance_called.append(game_id)

    engine = GameEngine(game_id=game.id, repo=repo, advance=fake_advance)
    engine.start()
    # Give the loop a chance to iterate
    await asyncio.sleep(0.05)
    assert advance_called == []
    await engine.stop()


async def test_engine_wakes_on_wake_signal(repo: SqliteRepo) -> None:
    """During a deadline wait, an explicit wake should short-circuit to advance."""
    clock = FakeClock(now=0)
    game = await _make_game(repo, phase=Phase.DAY_VOTE, deadline=10_000)
    call_event = asyncio.Event()

    async def fake_advance(game_id: str) -> None:
        from wolfbot.domain.models import Transition

        await repo.apply_transition(
            game_id,
            Transition(next_phase=Phase.GAME_OVER, next_day=1, new_deadline_epoch=None),
            expected_phase=Phase.DAY_VOTE,
        )
        call_event.set()

    engine = GameEngine(game_id=game.id, repo=repo, advance=fake_advance, clock=clock)
    engine.start()
    await asyncio.sleep(0.01)  # let the engine enter the deadline wait
    engine.wake()
    try:
        await asyncio.wait_for(call_event.wait(), timeout=2)
    except TimeoutError:
        pytest.fail("wake did not cause advance")
    await engine.stop()


async def test_engine_stops_on_game_over(repo: SqliteRepo) -> None:
    game = await _make_game(repo, phase=Phase.GAME_OVER, deadline=None)
    called: list[str] = []

    async def fake_advance(game_id: str) -> None:
        called.append(game_id)

    engine = GameEngine(game_id=game.id, repo=repo, advance=fake_advance)
    engine.start()
    # Engine should exit immediately without calling advance
    assert engine._task is not None  # type: ignore[attr-defined]
    await asyncio.wait_for(engine._task, timeout=1)  # type: ignore[attr-defined]
    assert called == []


async def test_engine_does_not_leak_stale_wake_across_transient_phases(
    repo: SqliteRepo,
) -> None:
    """Regression: `wake()` fired during a transient-phase advance must not make
    the next non-transient phase's `_wait_deadline_or_wake` return immediately.

    Mimics `GameService.advance`, which ends every successful advance with
    `wake()`. Across the SETUP→NIGHT_0→DAY_DISCUSSION chain, the pre-fix engine
    raced through all phases in milliseconds because each transient advance's
    wake leaked into the next iteration, making `wait_for` return on an
    already-set event.
    """
    from wolfbot.domain.models import Transition

    clock = FakeClock(now=0)
    game = await _make_game(repo, phase=Phase.SETUP, deadline=None)

    advance_calls: list[Phase] = []
    engine_ref: list[GameEngine] = []

    async def fake_advance(game_id: str) -> None:
        current = await repo.load_game(game_id)
        assert current is not None
        advance_calls.append(current.phase)
        if current.phase is Phase.SETUP:
            await repo.apply_transition(
                game_id,
                Transition(next_phase=Phase.NIGHT_0, next_day=0, new_deadline_epoch=None),
                expected_phase=Phase.SETUP,
            )
        elif current.phase is Phase.NIGHT_0:
            await repo.apply_transition(
                game_id,
                Transition(
                    next_phase=Phase.DAY_DISCUSSION,
                    next_day=1,
                    new_deadline_epoch=clock.now + 300,
                ),
                expected_phase=Phase.NIGHT_0,
            )
        engine_ref[0].wake()

    engine = GameEngine(game_id=game.id, repo=repo, advance=fake_advance, clock=clock)
    engine_ref.append(engine)
    engine.start()
    await asyncio.sleep(0.05)  # let SETUP→NIGHT_0→DAY_DISCUSSION cascade run

    assert advance_calls == [Phase.SETUP, Phase.NIGHT_0], (
        f"expected only transient advances, got {advance_calls!r} — engine leaked "
        "a stale wake into DAY_DISCUSSION wait"
    )
    assert engine._task is not None  # type: ignore[attr-defined]
    assert not engine._task.done(), (  # type: ignore[attr-defined]
        "engine should be parked in _wait_deadline_or_wake(300), not exited"
    )
    await engine.stop()


async def test_registry_wake_routes_to_engine(repo: SqliteRepo) -> None:
    registry = EngineRegistry()
    clock = FakeClock(now=0)
    game = await _make_game(repo, phase=Phase.DAY_VOTE, deadline=10_000)
    called: list[str] = []

    async def fake_advance(game_id: str) -> None:
        called.append(game_id)
        from wolfbot.domain.models import Transition

        await repo.apply_transition(
            game_id,
            Transition(next_phase=Phase.GAME_OVER, next_day=1, new_deadline_epoch=None),
            expected_phase=Phase.DAY_VOTE,
        )

    engine = GameEngine(game_id=game.id, repo=repo, advance=fake_advance, clock=clock)
    await registry.attach(engine)
    engine.start()
    await asyncio.sleep(0.01)
    registry.wake(game.id)
    try:
        await asyncio.wait_for(
            engine._task,  # type: ignore[attr-defined]
            timeout=2,
        )
    except TimeoutError:
        pytest.fail("registry wake did not route through")
    assert called == [game.id]
    await registry.stop_all()
