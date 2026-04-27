"""Game orchestrator.

Responsibilities:
  - Load a snapshot for the current phase, call the pure state_machine.plan_*(...)
  - Apply the returned Transition in order: permissions → commit → announcements → DMs
  - Wake the timer engine when submissions complete early
  - Handle `/wolf extend` and `/wolf force-skip` flows

The Discord/LLM sides are injected as Protocols so the orchestrator stays testable.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from random import Random
from typing import Protocol, runtime_checkable

from wolfbot.domain.enums import Phase, Role, SubmissionType, SubmitResult
from wolfbot.domain.models import (
    Game,
    NightAction,
    PendingDecision,
    Player,
    Seat,
    Transition,
    Vote,
)
from wolfbot.domain.rules import (
    compute_vote_result,
    legal_attack_targets,
    legal_divine_targets,
    legal_guard_targets,
    previous_guard_seat_for_night,
    resolve_wolf_attack,
)
from wolfbot.domain.state_machine import (
    plan_day_discussion_to_vote,
    plan_day_discussion_wait,
    plan_day_runoff_resolve,
    plan_day_vote_resolve,
    plan_extend_deadline,
    plan_night0,
    plan_night_resolve,
    plan_runoff_speech_to_runoff,
    plan_runoff_speech_wait,
    plan_setup,
)
from wolfbot.persistence.sqlite_repo import SqliteRepo

log = logging.getLogger(__name__)


@runtime_checkable
class DiscordAdapter(Protocol):
    """Discord operations the game service needs. Implemented by discord_service in M4."""

    async def apply_permissions(
        self, game: Game, seats: Sequence[Seat], players: Sequence[Player]
    ) -> None: ...
    async def kill_permissions(
        self, game: Game, seats: Sequence[Seat], seat_no: int, was_wolf: bool
    ) -> None: ...
    async def reconcile(
        self, game: Game, seats: Sequence[Seat], players: Sequence[Player]
    ) -> None: ...
    async def on_game_end(self, game: Game, seats: Sequence[Seat]) -> None: ...

    async def post_public(self, game: Game, text: str, kind: str) -> None: ...
    async def post_morning(self, game: Game, text: str) -> None: ...
    async def post_wolves_chat(self, game: Game, text: str, kind: str) -> None: ...
    async def send_private(self, game: Game, audience_seat: int, text: str, kind: str) -> None: ...

    async def send_vote_dms(
        self,
        game: Game,
        voters: Sequence[Player],
        candidates: Sequence[Seat],
        round_: int,
    ) -> None: ...
    async def send_night_action_dms(
        self,
        game: Game,
        actors: Sequence[Player],
        alive_players: Sequence[Player],
        seats: Sequence[Seat],
    ) -> None: ...

    async def announce_waiting(
        self,
        game: Game,
        pending: PendingDecision,
        seats: Sequence[Seat],
    ) -> None: ...


@runtime_checkable
class LLMAdapter(Protocol):
    async def submit_llm_night_actions(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        restrict_to_seats: frozenset[int] | None = None,
        unresolved_seats: frozenset[int] = frozenset(),
    ) -> None: ...
    async def submit_llm_votes(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        candidates: Sequence[int] | None,
        round_: int,
        restrict_to_seats: frozenset[int] | None = None,
    ) -> None: ...
    async def submit_llm_discussion_rounds(
        self, game: Game, players: Sequence[Player], seats: Sequence[Seat]
    ) -> None: ...
    async def submit_llm_runoff_candidate_speeches(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        tied_candidates: Sequence[int],
    ) -> None: ...


class WakeSink(Protocol):
    """The GameService calls this after a successful commit to notify the engine."""

    def wake(self, game_id: str) -> None: ...


class GameService:
    def __init__(
        self,
        repo: SqliteRepo,
        discord: DiscordAdapter,
        llm: LLMAdapter,
        wake: WakeSink,
        clock: Callable[[], int] = lambda: int(time.time()),
        rng: Random | None = None,
        on_reactive_phase_enter: Callable[[str], Awaitable[None]] | None = None,
        on_reactive_game_end: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.repo = repo
        self.discord = discord
        self.llm = llm
        self.wake = wake
        self.clock = clock
        self.rng = rng or Random()
        self._advance_locks: dict[str, asyncio.Lock] = {}
        self._on_reactive_phase_enter = on_reactive_phase_enter
        # Symmetric to `on_reactive_phase_enter`: invoked at natural end and
        # host abort so reactive_voice plumbing (NPC seat assignments, VC
        # joins) can be released.
        self._on_reactive_game_end = on_reactive_game_end

    def _lock_for(self, game_id: str) -> asyncio.Lock:
        lock = self._advance_locks.get(game_id)
        if lock is None:
            lock = asyncio.Lock()
            self._advance_locks[game_id] = lock
        return lock

    # ------------------------------------------------------------------ advance
    async def advance(self, game_id: str) -> None:
        """Drive one phase forward. Called by GameEngine on deadline or submission.

        Idempotent: if a competing advance already moved the phase, the optimistic lock
        in apply_transition returns False and we silently back off.
        """
        async with self._lock_for(game_id):
            await self._advance_once(game_id)

    async def _advance_once(self, game_id: str) -> None:
        game = await self.repo.load_game(game_id)
        if game is None:
            return
        if game.phase in (Phase.GAME_OVER, Phase.WAITING_HOST_DECISION, Phase.LOBBY):
            return

        seats = await self.repo.load_seats(game_id)
        players = await self.repo.load_players(game_id)
        now = self.clock()
        # Captured before commit so `_dispatch_submissions` can tell a fresh
        # phase entry from a same-phase grace re-commit (DAY_DISCUSSION /
        # DAY_RUNOFF_SPEECH wait pattern). Without this guard, the LLM round
        # task would be re-dispatched on every grace tick.
        previous_phase = game.phase

        transition = await self._plan_next(game, seats, players, now)
        if transition is None:
            return

        # 1. Discord permissions first — any failure aborts the advance (we'll retry).
        try:
            projected_game = game.model_copy(
                update={
                    "phase": transition.next_phase,
                    "day_number": transition.next_day,
                }
            )
            projected_players = _project_players(players, transition)
            await self.discord.apply_permissions(projected_game, seats, projected_players)
            for seat_no in transition.newly_dead_seats:
                was_wolf = any(
                    p.seat_no == seat_no and p.role is not None and p.role.name == "WEREWOLF"
                    for p in players
                )
                await self.discord.kill_permissions(game, seats, seat_no, was_wolf)
        except Exception:
            log.exception("permission application failed for game %s; aborting advance", game_id)
            return

        # 2. Commit transition (optimistic lock on expected_phase).
        ok = await self.repo.apply_transition(game_id, transition, expected_phase=game.phase)
        if not ok:
            log.info("optimistic lock miss on game %s; another advance ran", game_id)
            return

        new_game = await self.repo.load_game(game_id)
        assert new_game is not None

        # 3. Private role results, then public announcements in spec order.
        # plan_night_resolve emits public_logs as (MORNING, [PHASE_CHANGE |
        # VICTORY, ROLE_REVEAL]); the spec (prompts/IMPLEMENTATION_PROMPT.md
        # #338-349) requires medium/seer results to surface BEFORE the
        # morning announcement, and morning BEFORE victory/phase change.
        # Sending private_logs first + iterating public_logs in their stored
        # order satisfies both. MORNING entries are rendered via
        # post_morning() (☀️ decoration); every other kind goes through
        # post_public. public_logs contains exactly one MORNING entry per
        # dawn transition, so post_morning fires at most once.
        for entry in transition.private_logs:
            if entry.audience_seat is None:
                continue
            await self._safe_send_private(new_game, entry.audience_seat, entry.text, entry.kind)
        for entry in transition.public_logs:
            if entry.kind == "MORNING":
                await self._safe_post_morning(new_game, entry.text)
            else:
                await self._safe_post_public(new_game, entry.text, entry.kind)

        # 4. Announce WAITING status if we paused.
        if transition.requires_host_decision and transition.pending is not None:
            try:
                await self.discord.announce_waiting(new_game, transition.pending, seats)
            except Exception:
                log.exception("waiting announce failed for %s", game_id)

        # 4.5. Emit discussion_phase_summary when leaving a public-speech phase.
        if previous_phase in (Phase.DAY_DISCUSSION, Phase.DAY_RUNOFF_SPEECH):
            await self._emit_discussion_phase_summary(game, previous_phase)

        # 5. On entering DAY_VOTE / DAY_RUNOFF / NIGHT / DAY_DISCUSSION /
        #    DAY_RUNOFF_SPEECH, kick off DMs and LLM tasks.
        await self._dispatch_submissions(new_game, players, seats, transition, previous_phase)

        # 6. Victory handling: end game.
        if transition.victory is not None:
            try:
                await self.discord.on_game_end(new_game, seats)
            except Exception:
                log.exception("on_game_end failed for %s", game_id)
            if self._on_reactive_game_end is not None:
                try:
                    await self._on_reactive_game_end(game_id)
                except Exception:
                    log.exception(
                        "on_reactive_game_end failed for %s", game_id
                    )
            await self.repo.end_game(game_id, ended_at_epoch=self.clock())

        # 7. Wake the engine so it reschedules on the new deadline.
        self.wake.wake(game_id)

    async def _plan_next(
        self,
        game: Game,
        seats: Sequence[Seat],
        players: Sequence[Player],
        now: int,
    ) -> Transition | None:
        if game.phase is Phase.SETUP:
            return plan_setup(game, seats, self.rng, now)
        if game.phase is Phase.NIGHT_0:
            return plan_night0(game, players, seats, self.rng, now)
        if game.phase is Phase.DAY_DISCUSSION:
            # Advance only when BOTH the discussion deadline has passed AND every
            # alive LLM seat has completed both speech rounds. Otherwise either
            # park (no-op return None — engine sleeps to deadline) or commit a
            # short same-phase grace transition.
            #
            # Under reactive_voice mode there are no fixed rounds; LLMs speak
            # event-driven via SpeakArbiter, so the deadline is the sole gate.
            deadline_passed = game.deadline_epoch is not None and now >= game.deadline_epoch
            if game.discussion_mode == "reactive_voice":
                if deadline_passed:
                    return plan_day_discussion_to_vote(game, now)
                return None
            seats_by_no = {s.seat_no: s for s in seats}
            alive_llm_seats = [
                p.seat_no
                for p in players
                if p.alive
                and seats_by_no.get(p.seat_no) is not None
                and seats_by_no[p.seat_no].is_llm
            ]
            rounds_done = True
            for sn in alive_llm_seats:
                progress = await self.repo.load_llm_speech_progress(game.id, game.day_number, sn)
                if progress[3] < 2:  # discussion_rounds_done
                    rounds_done = False
                    break
            if deadline_passed and rounds_done:
                return plan_day_discussion_to_vote(game, now)
            if deadline_passed and not rounds_done:
                # Same-phase grace — engine sleeps DAY_DISCUSSION_GRACE seconds
                # then advances again. LLM completion will wake earlier.
                return plan_day_discussion_wait(game, now)
            # Not deadline_passed: either rounds done or not, just wait. Engine
            # sleeps until natural deadline; LLM wake re-triggers a check too.
            return None
        if game.phase is Phase.DAY_VOTE:
            votes = await self.repo.load_votes(game.id, day=game.day_number, round_=0)
            if not self._vote_resolution_due(game, players, votes, now):
                return None
            return plan_day_vote_resolve(game, players, seats, votes, game.force_skip_pending, now)
        if game.phase is Phase.DAY_RUNOFF_SPEECH:
            # Recompute tied candidates from round 0 votes — never stored.
            round0 = await self.repo.load_votes(game.id, day=game.day_number, round_=0)
            alive_set = {p.seat_no for p in players if p.alive}
            tied = compute_vote_result(round0, alive_set).tied
            seats_by_no = {s.seat_no: s for s in seats}
            tied_llm_seats = [
                sn for sn in tied if seats_by_no.get(sn) is not None and seats_by_no[sn].is_llm
            ]
            speeches_done = True
            for sn in tied_llm_seats:
                progress = await self.repo.load_llm_speech_progress(game.id, game.day_number, sn)
                if not progress[4]:  # runoff_speech_done
                    speeches_done = False
                    break
            if speeches_done:
                return plan_runoff_speech_to_runoff(game, seats_by_no, tied, now)
            deadline_passed = game.deadline_epoch is not None and now >= game.deadline_epoch
            if deadline_passed:
                return plan_runoff_speech_wait(game, now)
            return None
        if game.phase is Phase.DAY_RUNOFF:
            round0 = await self.repo.load_votes(game.id, day=game.day_number, round_=0)
            alive = {p.seat_no for p in players if p.alive}
            tied = compute_vote_result(round0, alive).tied
            round1 = await self.repo.load_votes(game.id, day=game.day_number, round_=1)
            if not self._vote_resolution_due(game, players, round1, now):
                return None
            return plan_day_runoff_resolve(
                game,
                players,
                seats,
                round1,
                tied,
                game.force_skip_pending,
                now,
            )
        if game.phase is Phase.NIGHT:
            actions = await self.repo.load_night_actions(game.id, day=game.day_number)
            prev = await self.repo.load_previous_guard(game.id)
            prev_seat = previous_guard_seat_for_night(prev, game.day_number)
            return plan_night_resolve(
                game,
                players,
                seats,
                actions,
                prev_seat,
                game.force_skip_pending,
                now,
            )
        return None

    async def _dispatch_submissions(
        self,
        new_game: Game,
        old_players: Sequence[Player],
        seats: Sequence[Seat],
        transition: Transition,
        previous_phase: Phase,
    ) -> None:
        # Post-transition player list (for dispatch decisions)
        players_after = [
            p.model_copy(update={"alive": False}) if p.seat_no in transition.newly_dead_seats else p
            for p in old_players
        ]
        if transition.next_phase is Phase.DAY_VOTE:
            alive_voters = [p for p in players_after if p.alive]
            alive_candidates = [s for s, p in zip(seats, players_after, strict=True) if p.alive]
            try:
                await self.discord.send_vote_dms(new_game, alive_voters, alive_candidates, round_=0)
            except Exception:
                log.exception("send_vote_dms failed for %s", new_game.id)
            try:
                await self.llm.submit_llm_votes(
                    new_game, alive_voters, seats, candidates=None, round_=0
                )
            except Exception:
                log.exception("llm vote submission failed for %s", new_game.id)
        elif transition.next_phase is Phase.DAY_RUNOFF_SPEECH:
            # Same-phase grace re-commit must not redispatch.
            if previous_phase is Phase.DAY_RUNOFF_SPEECH:
                return
            round0 = await self.repo.load_votes(new_game.id, day=new_game.day_number, round_=0)
            alive_set = {p.seat_no for p in players_after if p.alive}
            tied = list(compute_vote_result(round0, alive_set).tied)
            try:
                await self.llm.submit_llm_runoff_candidate_speeches(
                    new_game, players_after, seats, tied_candidates=tied
                )
            except Exception:
                log.exception("llm runoff candidate speech dispatch failed for %s", new_game.id)
        elif transition.next_phase is Phase.DAY_RUNOFF:
            alive_voters = [p for p in players_after if p.alive]
            # Candidates come from tied set — derived in _plan_next on the next advance;
            # for the DMs we need them now.
            round0 = await self.repo.load_votes(new_game.id, day=new_game.day_number, round_=0)
            alive_set = {p.seat_no for p in alive_voters}
            outcome = compute_vote_result(round0, alive_set)
            cand_seats = [s for s in seats if s.seat_no in outcome.tied]
            try:
                await self.discord.send_vote_dms(new_game, alive_voters, cand_seats, round_=1)
            except Exception:
                log.exception("send_vote_dms (runoff) failed for %s", new_game.id)
            try:
                await self.llm.submit_llm_votes(
                    new_game,
                    alive_voters,
                    seats,
                    candidates=list(outcome.tied),
                    round_=1,
                )
            except Exception:
                log.exception("llm runoff vote submission failed")
        elif transition.next_phase is Phase.NIGHT:
            alive_players = [p for p in players_after if p.alive]
            try:
                await self.discord.send_night_action_dms(
                    new_game, alive_players, alive_players, seats
                )
            except Exception:
                log.exception("send_night_action_dms failed for %s", new_game.id)
            try:
                await self.llm.submit_llm_night_actions(new_game, alive_players, seats)
            except Exception:
                log.exception("llm night action submission failed")
        elif transition.next_phase is Phase.DAY_DISCUSSION:
            # Same-phase grace re-commit must not redispatch.
            if previous_phase is Phase.DAY_DISCUSSION:
                return
            alive_players = [p for p in players_after if p.alive]
            # Mode-fixed dispatch: under reactive_voice we skip the rounds-mode
            # LLM batch entirely. Reactive speech is driven event-by-event by
            # SpeakArbiter via the WS server, not by the timer-driven engine.
            if new_game.discussion_mode == "reactive_voice":
                log.info(
                    "reactive_voice_phase_entered game=%s day=%d",
                    new_game.id,
                    new_game.day_number,
                )
                if self._on_reactive_phase_enter is not None:
                    try:
                        await self._on_reactive_phase_enter(new_game.id)
                    except Exception:
                        log.exception(
                            "reactive_phase_enter callback failed for %s", new_game.id
                        )
                return
            try:
                await self.llm.submit_llm_discussion_rounds(new_game, alive_players, seats)
            except Exception:
                log.exception("llm discussion rounds dispatch failed for %s", new_game.id)

    async def _emit_discussion_phase_summary(self, game: Game, phase: Phase) -> None:
        """Emit the discussion_phase_summary structured-log event.

        Called when transitioning away from DAY_DISCUSSION or DAY_RUNOFF_SPEECH.
        Best-effort: failures are logged but do not block the advance.
        """
        try:
            from wolfbot.domain.discussion import make_phase_id
            from wolfbot.services.discussion_phase_summary import emit_phase_summary
            from wolfbot.services.discussion_service import DiscussionService

            # Access the DiscussionService and repo through the LLM adapter
            # which holds a reference. The summary emitter needs both.
            ds: DiscussionService | None = getattr(self.llm, "discussion_service", None)
            if ds is None:
                return
            phase_id = make_phase_id(game.id, game.day_number, phase)
            await emit_phase_summary(
                repo=self.repo,
                discussion=ds,
                game_id=game.id,
                phase_id=phase_id,
                mode=game.discussion_mode,
            )
        except Exception:
            log.exception(
                "discussion_phase_summary emission failed for game=%s phase=%s",
                game.id,
                phase.value,
            )

    async def _safe_post_public(self, game: Game, text: str, kind: str) -> None:
        try:
            await self.discord.post_public(game, text, kind)
        except Exception:
            log.exception("post_public failed for %s", game.id)

    async def _safe_post_morning(self, game: Game, text: str) -> None:
        try:
            await self.discord.post_morning(game, text)
        except Exception:
            log.exception("post_morning failed for %s", game.id)

    async def _safe_send_private(self, game: Game, audience: int, text: str, kind: str) -> None:
        try:
            await self.discord.send_private(game, audience, text, kind)
        except Exception:
            log.exception("send_private failed for %s seat %s", game.id, audience)

    # --------------------------------------------------- submission callbacks
    async def submit_vote(
        self,
        game_id: str,
        voter_seat: int,
        target_seat: int | None,
        round_: int,
        day: int,
    ) -> SubmitResult:
        game = await self.repo.load_game(game_id)
        if game is None:
            return SubmitResult.GAME_NOT_FOUND
        expected_phase = Phase.DAY_VOTE if round_ == 0 else Phase.DAY_RUNOFF
        if game.phase is not expected_phase:
            log.info(
                "stale vote ignored: game=%s phase=%s expected=%s round=%s",
                game_id,
                game.phase,
                expected_phase,
                round_,
            )
            return SubmitResult.STALE_PHASE
        if game.day_number != day:
            log.info(
                "stale vote ignored (day mismatch): game=%s current_day=%s dm_day=%s",
                game_id,
                game.day_number,
                day,
            )
            return SubmitResult.STALE_PHASE
        players = await self.repo.load_players(game_id)
        alive = {p.seat_no for p in players if p.alive}
        if voter_seat not in alive:
            log.info("vote from dead/unknown seat %s ignored (game=%s)", voter_seat, game_id)
            return SubmitResult.VOTER_DEAD
        if target_seat is not None:
            if target_seat not in alive:
                log.info(
                    "vote targeting dead/unknown seat %s ignored (game=%s)",
                    target_seat,
                    game_id,
                )
                return SubmitResult.TARGET_DEAD
            if target_seat == voter_seat:
                log.info("self-vote seat=%s ignored (game=%s)", voter_seat, game_id)
                return SubmitResult.SELF_VOTE
            if round_ == 1:
                round0 = await self.repo.load_votes(game_id, day=game.day_number, round_=0)
                tied = set(compute_vote_result(round0, alive).tied)
                if target_seat not in tied:
                    log.info(
                        "runoff vote for non-tied target %s ignored (tied=%s game=%s)",
                        target_seat,
                        sorted(tied),
                        game_id,
                    )
                    return SubmitResult.ILLEGAL_TARGET
        await self.repo.insert_vote(
            Vote(
                game_id=game_id,
                day=game.day_number,
                round=round_,
                voter_seat=voter_seat,
                target_seat=target_seat,
                submitted_at=self.clock(),
            )
        )
        if await self._all_votes_in(game, round_):
            self.wake.wake(game_id)
        return SubmitResult.ACCEPTED

    async def submit_night_action(
        self,
        game_id: str,
        actor_seat: int,
        kind: SubmissionType,
        target_seat: int | None,
        day: int,
    ) -> SubmitResult:
        game = await self.repo.load_game(game_id)
        if game is None:
            return SubmitResult.GAME_NOT_FOUND
        if game.phase not in (Phase.NIGHT, Phase.NIGHT_0):
            log.info(
                "stale night action ignored: game=%s phase=%s kind=%s",
                game_id,
                game.phase,
                kind,
            )
            return SubmitResult.STALE_PHASE
        if game.day_number != day:
            log.info(
                "stale night action ignored (day mismatch): game=%s current_day=%s dm_day=%s kind=%s",
                game_id,
                game.day_number,
                day,
                kind,
            )
            return SubmitResult.STALE_PHASE
        players = await self.repo.load_players(game_id)
        actor = next((p for p in players if p.seat_no == actor_seat), None)
        if actor is None or not actor.alive:
            log.info(
                "night action from dead/unknown seat %s ignored (game=%s)",
                actor_seat,
                game_id,
            )
            return SubmitResult.ACTOR_DEAD
        role_for_kind = {
            SubmissionType.WOLF_ATTACK: Role.WEREWOLF,
            SubmissionType.SEER_DIVINE: Role.SEER,
            SubmissionType.KNIGHT_GUARD: Role.KNIGHT,
        }.get(kind)
        if role_for_kind is None or actor.role is not role_for_kind:
            log.info(
                "night action role mismatch: seat=%s role=%s kind=%s ignored (game=%s)",
                actor_seat,
                actor.role,
                kind,
                game_id,
            )
            return SubmitResult.ROLE_MISMATCH
        # Service-boundary defense: night actions never legitimately carry
        # target_seat=None. UI requires a Select pick; LLM resolves with
        # allow_none=False (fallback to random legal target). A null target
        # would otherwise be saved and later make plan_night_resolve treat
        # the actor as "submitted" while silently dropping the effect.
        # force-skip "no action" is handled by plan_night_resolve(force_skip=True)
        # via missing_seats — it does not flow through this submission path.
        if target_seat is None:
            log.info(
                "night action with null target rejected: seat=%s kind=%s (game=%s)",
                actor_seat,
                kind,
                game_id,
            )
            return SubmitResult.ILLEGAL_TARGET
        if kind is SubmissionType.WOLF_ATTACK:
            legal = legal_attack_targets(players, actor_seat)
        elif kind is SubmissionType.SEER_DIVINE:
            legal = legal_divine_targets(players, actor_seat)
        else:  # KNIGHT_GUARD
            prev = await self.repo.load_previous_guard(game_id)
            prev_target = previous_guard_seat_for_night(prev, game.day_number)
            legal = legal_guard_targets(players, actor_seat, prev_target)
        if target_seat not in legal:
            log.info(
                "illegal night target seat=%s kind=%s target=%s legal=%s ignored (game=%s)",
                actor_seat,
                kind,
                target_seat,
                legal,
                game_id,
            )
            return SubmitResult.ILLEGAL_TARGET
        await self.repo.insert_night_action(
            NightAction(
                game_id=game_id,
                day=game.day_number,
                actor_seat=actor_seat,
                kind=kind,
                target_seat=target_seat,
                submitted_at=self.clock(),
            )
        )
        if await self._all_night_actions_in(game):
            self.wake.wake(game_id)
        return SubmitResult.ACCEPTED

    async def resend_pending_dms(self, game_id: str) -> None:
        """Re-send DM UIs to humans and re-dispatch LLM tasks for pending seats.

        Called from recovery after engine reattach, and from `host_extend` when
        a WAITING phase is resumed. Needed because VoteView/NightActionView
        hold their submit callback in an in-memory closure that does not
        survive a bot restart, and because `/wolf extend` does not trigger a
        phase-entry Transition (so `_dispatch_submissions` never re-fires).

        No-ops for phases that never had DM UIs (LOBBY/SETUP/NIGHT_0/
        DAY_DISCUSSION/WAITING_HOST_DECISION/GAME_OVER). `send_vote_dms` /
        `send_night_action_dms` already filter LLM seats internally, so humans
        get re-DMed here; LLMs get re-dispatched via the LLMAdapter with
        `restrict_to_seats` set to exactly the still-pending LLM seats. The
        in-loop "already submitted?" guard in the LLM dispatcher makes this
        safe even if the original task is still running.

        At NIGHT, we union "missing" (never submitted) with "unresolved"
        (submitted but split) so wolves who picked different targets also get
        re-DMed — otherwise a split lockout could only be broken by force-skip.
        """
        from wolfbot.services.submission_snapshot import (
            missing_submitters,
            unresolved_submitters,
        )

        game = await self.repo.load_game(game_id)
        if game is None:
            return
        if game.phase not in (Phase.DAY_VOTE, Phase.DAY_RUNOFF, Phase.NIGHT):
            return

        seats = await self.repo.load_seats(game_id)
        seats_by_no = {s.seat_no: s for s in seats}
        players = await self.repo.load_players(game_id)
        missing_by_kind = await missing_submitters(self.repo, game, players)

        if game.phase in (Phase.DAY_VOTE, Phase.DAY_RUNOFF):
            kind = (
                SubmissionType.VOTE if game.phase is Phase.DAY_VOTE else SubmissionType.RUNOFF_VOTE
            )
            missing = set(missing_by_kind.get(kind, ()))
            if not missing:
                return
            voters = [p for p in players if p.seat_no in missing]
            round_ = 0 if game.phase is Phase.DAY_VOTE else 1
            alive_seats = {p.seat_no for p in players if p.alive}
            if game.phase is Phase.DAY_VOTE:
                candidates = [s for s in seats if s.seat_no in alive_seats]
                candidate_seat_nos: list[int] | None = None
            else:
                round0 = await self.repo.load_votes(game_id, day=game.day_number, round_=0)
                tied = set(compute_vote_result(round0, alive_seats).tied)
                candidates = [s for s in seats if s.seat_no in tied]
                candidate_seat_nos = list(tied)
            try:
                await self.discord.send_vote_dms(game, voters, candidates, round_=round_)
            except Exception:
                log.exception("resend_pending_dms vote failed for %s", game_id)
            llm_missing = frozenset(
                sn for sn in missing if seats_by_no.get(sn) is not None and seats_by_no[sn].is_llm
            )
            if llm_missing:
                try:
                    await self.llm.submit_llm_votes(
                        game,
                        players,
                        seats,
                        candidates=candidate_seat_nos,
                        round_=round_,
                        restrict_to_seats=llm_missing,
                    )
                except Exception:
                    log.exception("resend_pending_dms llm vote dispatch failed for %s", game_id)
            return

        # NIGHT
        unresolved_by_kind = await unresolved_submitters(self.repo, game, players)
        seats_to_dm: set[int] = set()
        for seats_tuple in missing_by_kind.values():
            seats_to_dm.update(seats_tuple)
        unresolved_flat: set[int] = set()
        for seats_tuple in unresolved_by_kind.values():
            unresolved_flat.update(seats_tuple)
        seats_to_dm.update(unresolved_flat)
        if not seats_to_dm:
            return
        actors = [p for p in players if p.seat_no in seats_to_dm]
        alive_players = [p for p in players if p.alive]
        try:
            await self.discord.send_night_action_dms(game, actors, alive_players, seats)
        except Exception:
            log.exception("resend_pending_dms night failed for %s", game_id)
        llm_seats = frozenset(
            sn for sn in seats_to_dm if seats_by_no.get(sn) is not None and seats_by_no[sn].is_llm
        )
        llm_unresolved = frozenset(sn for sn in unresolved_flat if sn in llm_seats)
        if llm_seats:
            try:
                await self.llm.submit_llm_night_actions(
                    game,
                    players,
                    seats,
                    restrict_to_seats=llm_seats,
                    unresolved_seats=llm_unresolved,
                )
            except Exception:
                log.exception("resend_pending_dms llm night dispatch failed for %s", game_id)

    async def resume_llm_speech_progress(self, game_id: str) -> None:
        """Re-dispatch LLM speech tasks if DAY_DISCUSSION/DAY_RUNOFF_SPEECH
        progress is incomplete after a bot restart.

        Called from `RecoveryService._recover_one` after `resend_pending_dms`.
        Idempotent: the LLM dispatcher's per-seat skip (rounds_done >= round_idx
        / runoff_speech_done already True) makes redundant calls a no-op. For
        any other phase this is a no-op.
        """
        game = await self.repo.load_game(game_id)
        if game is None:
            return
        # Under reactive_voice, LLM speech is driven event-by-event by the
        # SpeakArbiter via the WS server — there are no fixed rounds to
        # resume. Skip entirely so we don't spawn a legacy two-round batch
        # that would violate the per-game mode contract.
        if game.discussion_mode == "reactive_voice":
            return
        seats = await self.repo.load_seats(game_id)
        seats_by_no = {s.seat_no: s for s in seats}
        players = await self.repo.load_players(game_id)
        if game.phase is Phase.DAY_DISCUSSION:
            alive_llm_players = [
                p
                for p in players
                if p.alive
                and seats_by_no.get(p.seat_no) is not None
                and seats_by_no[p.seat_no].is_llm
            ]
            if not alive_llm_players:
                return
            for p in alive_llm_players:
                progress = await self.repo.load_llm_speech_progress(
                    game_id, game.day_number, p.seat_no
                )
                if progress[3] < 2:
                    break
            else:
                return  # everyone done — nothing to resume
            try:
                await self.llm.submit_llm_discussion_rounds(game, alive_llm_players, seats)
            except Exception:
                log.exception("resume discussion rounds failed for %s", game_id)
            return
        if game.phase is Phase.DAY_RUNOFF_SPEECH:
            round0 = await self.repo.load_votes(game_id, day=game.day_number, round_=0)
            alive_set = {p.seat_no for p in players if p.alive}
            tied = list(compute_vote_result(round0, alive_set).tied)
            tied_llm_players = [
                p
                for p in players
                if p.seat_no in tied
                and seats_by_no.get(p.seat_no) is not None
                and seats_by_no[p.seat_no].is_llm
            ]
            if not tied_llm_players:
                return
            for p in tied_llm_players:
                progress = await self.repo.load_llm_speech_progress(
                    game_id, game.day_number, p.seat_no
                )
                if not progress[4]:
                    break
            else:
                return
            try:
                await self.llm.submit_llm_runoff_candidate_speeches(
                    game, players, seats, tied_candidates=tied
                )
            except Exception:
                log.exception("resume runoff candidate speeches failed for %s", game_id)

    async def _all_votes_in(self, game: Game, round_: int) -> bool:
        players = await self.repo.load_players(game.id)
        votes = await self.repo.load_votes(game.id, day=game.day_number, round_=round_)
        alive = {p.seat_no for p in players if p.alive}
        submitted = {v.voter_seat for v in votes if v.voter_seat in alive}
        return alive.issubset(submitted)

    def _vote_resolution_due(
        self,
        game: Game,
        players: Sequence[Player],
        votes: Sequence[Vote],
        now: int,
    ) -> bool:
        # Pure data-in / bool-out so _plan_next can reuse loaded snapshot without re-IO.
        # Without this guard a stale wake or partial LLM-vote completion would call
        # plan_day_vote_resolve before the deadline and the domain function — which
        # has no knowledge of time — would treat any missing voter as a host-decision
        # trigger. Resolution is only due when force-skip, deadline, or all-voted holds.
        if game.force_skip_pending:
            return True
        if game.deadline_epoch is not None and now >= game.deadline_epoch:
            return True
        alive = {p.seat_no for p in players if p.alive}
        submitted = {v.voter_seat for v in votes if v.voter_seat in alive}
        return alive.issubset(submitted)

    async def _all_night_actions_in(self, game: Game) -> bool:
        players = await self.repo.load_players(game.id)
        seats = await self.repo.load_seats(game.id)
        actions = await self.repo.load_night_actions(game.id, day=game.day_number)
        expected: set[tuple[int, SubmissionType]] = set()
        from wolfbot.domain.enums import Role

        for p in players:
            if not p.alive or p.role is None:
                continue
            if p.role is Role.SEER:
                expected.add((p.seat_no, SubmissionType.SEER_DIVINE))
            elif p.role is Role.KNIGHT:
                expected.add((p.seat_no, SubmissionType.KNIGHT_GUARD))
            elif p.role is Role.WEREWOLF:
                expected.add((p.seat_no, SubmissionType.WOLF_ATTACK))
        got = {(a.actor_seat, a.kind) for a in actions}
        if not expected.issubset(got):
            return False
        # All required actors submitted. Mirror plan_night_resolve's human-wolf
        # priority so a mixed human+LLM wolf disagreement (which the deadline
        # resolver would settle in the human's favor) wakes early instead of
        # idling for the rest of NIGHT_DURATION. Same-kind splits stay split
        # and continue to wait until the deadline / WAITING_HOST_DECISION.
        seats_by_no = {s.seat_no: s for s in seats}
        wolf_actions = [a for a in actions if a.kind is SubmissionType.WOLF_ATTACK]
        alive_wolves = [p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF]
        human_wolf_seats = [
            w for w in alive_wolves if seats_by_no.get(w) is not None and not seats_by_no[w].is_llm
        ]
        attack = resolve_wolf_attack(
            wolf_actions, alive_wolves, force_skip=False, human_wolf_seats=human_wolf_seats
        )
        return not attack.split

    # ------------------------------------------------------ host commands
    async def host_extend(self, game_id: str, extra_seconds: int) -> bool:
        """/wolf extend. Valid only while WAITING_HOST_DECISION."""
        game = await self.repo.load_game(game_id)
        if game is None or game.phase is not Phase.WAITING_HOST_DECISION:
            return False
        pending = await self.repo.load_pending_decision(game_id)
        if pending is None:
            return False
        # Restore the paused phase and reset deadline.
        game_restored = game.model_copy(update={"phase": pending.phase})
        now = self.clock()
        transition = plan_extend_deadline(game_restored, extra_seconds, now)
        ok = await self.repo.apply_transition(
            game_id, transition, expected_phase=Phase.WAITING_HOST_DECISION
        )
        if ok:
            await self.repo.clear_pending_decision(game_id)
            await self.resend_pending_dms(game_id)
            self.wake.wake(game_id)
        return ok

    async def host_force_skip(self, game_id: str) -> bool:
        """/wolf force-skip. Valid only while WAITING_HOST_DECISION.

        Sets `force_skip_pending=1` and swaps phase back to the paused phase in a
        single transaction (`Transition.set_force_skip=True`), then wakes. If
        `/wolf extend` wins the race, apply_transition's optimistic lock fails
        and the flag set is rolled back too — no residual `force_skip_pending`.
        """
        game = await self.repo.load_game(game_id)
        if game is None or game.phase is not Phase.WAITING_HOST_DECISION:
            return False
        pending = await self.repo.load_pending_decision(game_id)
        if pending is None:
            return False
        t = Transition(
            next_phase=pending.phase,
            next_day=game.day_number,
            new_deadline_epoch=self.clock(),  # deadline = now → advance fires immediately
            set_force_skip=True,
        )
        ok = await self.repo.apply_transition(
            game_id, t, expected_phase=Phase.WAITING_HOST_DECISION
        )
        if ok:
            await self.repo.clear_pending_decision(game_id)
            self.wake.wake(game_id)
        return ok

    async def host_abort(self, game_id: str) -> bool:
        game = await self.repo.load_game(game_id)
        if game is None or game.ended_at is not None:
            return False
        seats = await self.repo.load_seats(game_id)
        try:
            await self.discord.on_game_end(game, seats)
        except Exception:
            log.exception("on_game_end failed during abort %s", game_id)
        if self._on_reactive_game_end is not None:
            try:
                await self._on_reactive_game_end(game_id)
            except Exception:
                log.exception(
                    "on_reactive_game_end failed during abort %s", game_id
                )
        await self.repo.end_game(game_id, ended_at_epoch=self.clock())
        self.wake.wake(game_id)
        return True


def new_game_id() -> str:
    return uuid.uuid4().hex[:12]


def _project_players(players: Sequence[Player], transition: Transition) -> list[Player]:
    """Apply a Transition's player_updates to a copy of `players`."""
    updates_by_seat = {u.seat_no: u for u in transition.player_updates}
    projected: list[Player] = []
    for p in players:
        upd = updates_by_seat.get(p.seat_no)
        if upd is None:
            projected.append(p)
            continue
        changes: dict[str, object] = {}
        if upd.role is not None:
            changes["role"] = upd.role
        if upd.alive is not None:
            changes["alive"] = upd.alive
        if upd.death_cause is not None:
            changes["death_cause"] = upd.death_cause
        if upd.death_day is not None:
            changes["death_day"] = upd.death_day
        projected.append(p.model_copy(update=changes))
    return projected
