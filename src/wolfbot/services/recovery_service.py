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
    async def announce_recovery(
        self, game: Game, pending: PendingDecision | None) -> None: ...


class ReactiveVoiceRecoverySweep(Protocol):
    """Callable that closes in-flight npc_speak_requests and npc_playback_events."""

    async def __call__(self, game_id: str) -> None: ...


class ReactiveVoiceReenter(Protocol):
    """Callable that triggers arbiter dispatch after recovery."""

    async def __call__(self, game_id: str) -> None: ...


class RecoveryService:
    def __init__(
        self,
        repo: SqliteRepo,
        game_service: GameService,
        registry: EngineRegistry,
        discord: RecoveryDiscordAdapter,
        clock: Callable[[], int] = lambda: int(time.time()),
        reactive_voice_sweep: ReactiveVoiceRecoverySweep | None = None,
        reactive_voice_reenter: ReactiveVoiceReenter | None = None,
    ) -> None:
        self.repo = repo
        self.game_service = game_service
        self.registry = registry
        self.discord = discord
        self.clock = clock
        self._reactive_voice_sweep = reactive_voice_sweep
        self._reactive_voice_reenter = reactive_voice_reenter

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

        # Step 2.5: sweep open reactive_voice audit rows so in-flight requests
        # and playback windows from before the restart are closed with
        # failure_reason=master_restart (npc-voice-pipeline spec §Recovery).
        if game.discussion_mode == "reactive_voice" and self._reactive_voice_sweep is not None:
            try:
                await self._reactive_voice_sweep(game.id)
            except Exception:
                log.exception(
                    "reactive_voice recovery sweep failed for %s", game.id)

        # Step 3: attach and start engine. Pass the recovery clock so tests
        # using FakeClock get deterministic timing — otherwise the engine
        # uses time.time() and may race ahead of the test's logical clock,
        # advancing the phase before assertions run.
        engine = GameEngine(
            game_id=game.id,
            repo=self.repo,
            advance=self.game_service.advance,
            clock=self.clock,
        )
        await self.registry.attach(engine)
        engine.start()

        # Step 4: re-send DM UIs so humans who had a stale/dead DM from before
        # the restart can still submit. No-op for non-submission phases.
        try:
            await self.game_service.resend_pending_dms(game.id)
        except Exception:
            log.exception(
                "resend_pending_dms during recovery failed for %s", game.id)

        # Step 5: resume LLM speech progress for DAY_DISCUSSION /
        # DAY_RUNOFF_SPEECH if any per-seat round/runoff_speech_done is still
        # incomplete. Idempotent; no-op for any other phase.
        try:
            await self.game_service.resume_llm_speech_progress(game.id)
        except Exception:
            log.exception(
                "resume_llm_speech_progress during recovery failed for %s", game.id)

        # Step 6: re-enter the arbiter for reactive_voice games in a public
        # speech phase so NPCs resume speaking after restart.
        if (
            game.discussion_mode == "reactive_voice"
            and game.phase in (Phase.DAY_DISCUSSION, Phase.DAY_RUNOFF_SPEECH)
            and self._reactive_voice_reenter is not None
        ):
            try:
                await self._reactive_voice_reenter(game.id)
            except Exception:
                log.exception(
                    "reactive_voice reenter dispatch failed for %s", game.id)
