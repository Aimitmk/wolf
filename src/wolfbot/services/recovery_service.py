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
from wolfbot.domain.models import (
    Game,
    PendingDecision,
    PendingSubmission,
    Player,
    Seat,
    Transition,
)
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.game_service import GameService
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
            pending = await _derive_pending(self.repo, game, players, self.clock())
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


async def _derive_pending(
    repo: SqliteRepo, game: Game, players: Sequence[Player], now: int
) -> PendingDecision:
    """Figure out who actually still owes a submission for the frozen phase.

    Reads the real `votes` / `night_actions` rows, subtracts submitted seats from
    expected seats (role-appropriate, day-appropriate), and returns the decision
    with a full `submissions` breakdown so the host UI can display every type.
    """
    alive_seats = {p.seat_no for p in players if p.alive}

    if game.phase is Phase.DAY_VOTE or game.phase is Phase.DAY_RUNOFF:
        round_ = 0 if game.phase is Phase.DAY_VOTE else 1
        votes = await repo.load_votes(game.id, game.day_number, round_=round_)
        voted = {v.voter_seat for v in votes}
        missing = tuple(sorted(alive_seats - voted))
        kind = SubmissionType.VOTE if game.phase is Phase.DAY_VOTE else SubmissionType.RUNOFF_VOTE
        return PendingDecision(
            game_id=game.id,
            phase=game.phase,
            day=game.day_number,
            required_submission=kind,
            missing_seats=missing,
            submissions=(PendingSubmission(submission_type=kind, missing_seats=missing),),
            created_at=now,
        )

    if game.phase is Phase.NIGHT:
        actions = await repo.load_night_actions(game.id, game.day_number)
        submitted_by_kind: dict[SubmissionType, set[int]] = {
            SubmissionType.WOLF_ATTACK: set(),
            SubmissionType.SEER_DIVINE: set(),
            SubmissionType.KNIGHT_GUARD: set(),
        }
        for a in actions:
            if a.kind in submitted_by_kind:
                submitted_by_kind[a.kind].add(a.actor_seat)

        # Knight only submits starting night 1 (state_machine gate).
        kinds: list[tuple[SubmissionType, Role]] = [
            (SubmissionType.WOLF_ATTACK, Role.WEREWOLF),
            (SubmissionType.SEER_DIVINE, Role.SEER),
        ]
        if game.day_number >= 1:
            kinds.append((SubmissionType.KNIGHT_GUARD, Role.KNIGHT))

        subs: list[PendingSubmission] = []
        for kind, required_role in kinds:
            expected = {p.seat_no for p in players if p.alive and p.role is required_role}
            missing_for_kind = tuple(sorted(expected - submitted_by_kind[kind]))
            if missing_for_kind:
                subs.append(PendingSubmission(submission_type=kind, missing_seats=missing_for_kind))

        if not subs:
            # Deadline fired with all submissions already in (race condition);
            # park on WOLF_ATTACK as the nominal primary with an empty seat list.
            subs = [PendingSubmission(submission_type=SubmissionType.WOLF_ATTACK, missing_seats=())]

        primary = subs[0]
        return PendingDecision(
            game_id=game.id,
            phase=Phase.NIGHT,
            day=game.day_number,
            required_submission=primary.submission_type,
            missing_seats=primary.missing_seats,
            submissions=tuple(subs),
            created_at=now,
        )

    # Fallback: no derivable pending (shouldn't happen for timer-driven phases)
    return PendingDecision(
        game_id=game.id,
        phase=game.phase,
        day=game.day_number,
        required_submission=SubmissionType.VOTE,
        missing_seats=(),
        submissions=(),
        created_at=now,
    )
