"""Deadline-based per-game engine.

Each active game owns one `GameEngine`. The engine runs in a long-lived asyncio.Task.
It waits until the game's current `deadline_epoch` OR until an explicit `wake` event,
then asks `GameService.advance()` to drive the phase forward.

Transient phases (SETUP, NIGHT_0) have `deadline_epoch=None`; the loop advances them
immediately instead of sleeping.

The engine parks indefinitely while the game is in `WAITING_HOST_DECISION` and wakes
only when a host command wakes it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from wolfbot.domain.enums import Phase
from wolfbot.persistence.sqlite_repo import SqliteRepo

log = logging.getLogger(__name__)


class GameEngine:
    def __init__(
        self,
        game_id: str,
        repo: SqliteRepo,
        advance: Callable[[str], Awaitable[None]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.game_id = game_id
        self.repo = repo
        self._advance = advance
        self._clock = clock
        self._wake = asyncio.Event()
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"engine-{self.game_id}")

    async def stop(self) -> None:
        self._stopped.set()
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()

    async def _run(self) -> None:
        while not self._stopped.is_set():
            # Drop any wake left set by a prior iteration — notably the chain
            # of transient-phase advances (SETUP→NIGHT_0→DAY_DISCUSSION) each
            # calls `wake()` at the end of `GameService.advance`, leaving the
            # event set when we finally reach a deadline-bearing phase. Without
            # this clear, `_wait_deadline_or_wake(300)` would return instantly
            # on a pre-set event and the engine would race through DAY_DISCUSSION
            # and DAY_VOTE in milliseconds. The re-read below captures whatever
            # state those wakes were signaling, so discarding them here is
            # lossless. Wakes fired AFTER this line (during load_game's awaits
            # or inside _wait_deadline_or_wake) still work — they set the event
            # before we await it.
            self._wake.clear()
            try:
                game = await self.repo.load_game(self.game_id)
            except Exception:
                log.exception("engine %s: load_game failed", self.game_id)
                await asyncio.sleep(1)
                continue
            if game is None:
                return
            if game.phase is Phase.GAME_OVER or game.ended_at is not None:
                return

            if game.phase is Phase.WAITING_HOST_DECISION:
                await self._wait_wake()
                continue

            if game.deadline_epoch is None:
                # transient phase → advance immediately
                await self._safe_advance()
                continue

            now = self._clock()
            remaining = game.deadline_epoch - now
            if remaining <= 0:
                await self._safe_advance()
                continue
            await self._wait_deadline_or_wake(remaining)
            if self._stopped.is_set():
                return
            await self._safe_advance()

    async def _wait_wake(self) -> None:
        await self._wake.wait()
        self._wake.clear()

    async def _wait_deadline_or_wake(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except TimeoutError:
            pass
        else:
            self._wake.clear()

    async def _safe_advance(self) -> None:
        try:
            await self._advance(self.game_id)
        except Exception:
            log.exception("engine %s: advance raised; retrying after 1s", self.game_id)
            await asyncio.sleep(1)


class EngineRegistry:
    """Owns every active GameEngine; also the `wake` sink passed to GameService."""

    def __init__(self) -> None:
        self._engines: dict[str, GameEngine] = {}

    async def attach(self, engine: GameEngine) -> None:
        """Register `engine`, stopping any prior engine for the same game first.

        Recovery can re-fire (e.g. Discord reconnect retriggers on_ready), so the
        registry must not leak orphan tasks that keep driving a game after they've
        been replaced. Stopping before replacement makes `attach()` idempotent.
        """
        existing = self._engines.pop(engine.game_id, None)
        if existing is not None and existing is not engine:
            try:
                await existing.stop()
            except Exception:
                log.exception("attach: failed to stop existing engine for %s", engine.game_id)
        self._engines[engine.game_id] = engine

    def detach(self, game_id: str) -> GameEngine | None:
        return self._engines.pop(game_id, None)

    def wake(self, game_id: str) -> None:
        engine = self._engines.get(game_id)
        if engine is not None:
            engine.wake()

    async def stop_all(self) -> None:
        engines = list(self._engines.values())
        self._engines.clear()
        await asyncio.gather(*(e.stop() for e in engines), return_exceptions=True)
