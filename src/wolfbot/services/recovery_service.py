"""Bot-restart recovery.

On startup we scan all unended games. For each:
  - If the stored deadline is in the past, force into WAITING_HOST_DECISION so we don't
    secretly resolve actions the host never got to review.
  - Else, reconcile channel permissions against current internal state and attach a
    GameEngine to resume.
Errors from one game never bleed into another — each is recovered in isolation.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from wolfbot.domain.enums import Phase
from wolfbot.domain.models import (
    Game,
    PendingDecision,
    Player,
    Seat,
    Transition,
)
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.game_service import GameService
from wolfbot.services.submission_snapshot import derive_pending
from wolfbot.services.timer_service import EngineRegistry, GameEngine

log = logging.getLogger(__name__)


@runtime_checkable
class RecoveryDiscordAdapter(Protocol):
    async def reconcile(
        self, game: Game, seats: Sequence[Seat], players: Sequence[Player]
    ) -> None: ...
    async def announce_recovery(self, game: Game, pending: PendingDecision | None) -> None: ...


class RecoveryService:
    def __init__(
        self,
        repo: SqliteRepo,
        game_service: GameService,
        registry: EngineRegistry,
        discord: RecoveryDiscordAdapter,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self.repo = repo
        self.game_service = game_service
        self.registry = registry
        self.discord = discord
        self.clock = clock

    async def recover_all(self) -> list[str]:
        games = await self.repo.load_active_games()
        recovered: list[str] = []
        for g in games:
            try:
                await self._recover_one(g)
                recovered.append(g.id)
            except Exception:
                log.exception("recovery failed for game %s", g.id)
        return recovered

    async def _recover_one(self, game: Game) -> None:
        seats = await self.repo.load_seats(game.id)
        players = await self.repo.load_players(game.id)

        # Step 1: if a submission-driven phase's deadline expired, force WAITING so the
        # host can review missing submissions. Non-submission phases (DAY_DISCUSSION) are
        # safe to auto-advance, so we leave them alone and let the engine do its thing.
        submission_phases = (Phase.DAY_VOTE, Phase.DAY_RUNOFF, Phase.NIGHT)
        if (
            game.phase in submission_phases
            and game.deadline_epoch is not None
            and self.clock() > game.deadline_epoch
        ):
            pending = await derive_pending(self.repo, game, players, self.clock())
            transition = Transition(
                next_phase=Phase.WAITING_HOST_DECISION,
                next_day=game.day_number,
                new_deadline_epoch=None,
                requires_host_decision=True,
                pending=pending,
            )
            ok = await self.repo.apply_transition(game.id, transition, expected_phase=game.phase)
            if ok:
                game = (await self.repo.load_game(game.id)) or game

        # Step 2: reconcile Discord state against current internal state.
        try:
            await self.discord.reconcile(game, seats, players)
        except Exception:
            log.exception("reconcile failed during recovery of %s", game.id)

        current_pending = await self.repo.load_pending_decision(game.id)
        try:
            await self.discord.announce_recovery(game, current_pending)
        except Exception:
            log.exception("announce_recovery failed for %s", game.id)

        # Step 3: attach and start engine.
        engine = GameEngine(
            game_id=game.id,
            repo=self.repo,
            advance=self.game_service.advance,
        )
        await self.registry.attach(engine)
        engine.start()

        # Step 4: re-send DM UIs so humans who had a stale/dead DM from before
        # the restart can still submit. No-op for non-submission phases.
        try:
            await self.game_service.resend_pending_dms(game.id)
        except Exception:
            log.exception("resend_pending_dms during recovery failed for %s", game.id)
