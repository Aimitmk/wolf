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

from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import Game, PendingDecision, Player, Seat, Transition
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.game_service import GameService
from wolfbot.services.timer_service import EngineRegistry, GameEngine

log = logging.getLogger(__name__)


@runtime_checkable
class RecoveryDiscordAdapter(Protocol):
    async def reconcile(
        self, game: Game, seats: Sequence[Seat], players: Sequence[Player]
    ) -> None: ...
    async def announce_recovery(
        self, game: Game, pending: PendingDecision | None
    ) -> None: ...


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
            pending = _derive_pending(game, players, self.clock())
            transition = Transition(
                next_phase=Phase.WAITING_HOST_DECISION,
                next_day=game.day_number,
                new_deadline_epoch=None,
                requires_host_decision=True,
                pending=pending,
            )
            ok = await self.repo.apply_transition(
                game.id, transition, expected_phase=game.phase
            )
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
        self.registry.attach(engine)
        engine.start()


def _derive_pending(
    game: Game, players: Sequence[Player], now: int
) -> PendingDecision:
    """Figure out missing submitters for the currently-frozen phase."""
    if game.phase is Phase.DAY_VOTE or game.phase is Phase.DAY_RUNOFF:
        alive = [p.seat_no for p in players if p.alive]
        required = (
            SubmissionType.VOTE if game.phase is Phase.DAY_VOTE else SubmissionType.RUNOFF_VOTE
        )
        return PendingDecision(
            game_id=game.id,
            phase=game.phase,
            day=game.day_number,
            required_submission=required,
            missing_seats=tuple(sorted(alive)),  # treat all alive as pending; caller can refine
            created_at=now,
        )
    if game.phase is Phase.NIGHT:
        expected: list[int] = []
        for p in players:
            if not p.alive or p.role is None:
                continue
            if p.role in (Role.WEREWOLF, Role.SEER, Role.KNIGHT):
                expected.append(p.seat_no)
        return PendingDecision(
            game_id=game.id,
            phase=Phase.NIGHT,
            day=game.day_number,
            required_submission=SubmissionType.WOLF_ATTACK,
            missing_seats=tuple(sorted(expected)),
            created_at=now,
        )
    # Fallback: no derivable pending (shouldn't happen for timer-driven phases)
    return PendingDecision(
        game_id=game.id,
        phase=game.phase,
        day=game.day_number,
        required_submission=SubmissionType.VOTE,
        missing_seats=(),
        created_at=now,
    )
