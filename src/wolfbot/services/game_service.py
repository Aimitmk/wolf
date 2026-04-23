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
from collections.abc import Callable, Sequence
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
)
from wolfbot.domain.state_machine import (
    plan_day_discussion_to_vote,
    plan_day_runoff_resolve,
    plan_day_vote_resolve,
    plan_extend_deadline,
    plan_night0,
    plan_night_resolve,
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
    async def send_private(self, game: Game, audience_seat: int, text: str, kind: str) -> None: ...

    async def send_vote_dms(
        self,
        game: Game,
        voters: Sequence[Player],
        candidates: Sequence[Seat],
        round_: int,
    ) -> None: ...
    async def send_night_action_dms(
        self, game: Game, players: Sequence[Player], seats: Sequence[Seat]
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
        self, game: Game, players: Sequence[Player], seats: Sequence[Seat]
    ) -> None: ...
    async def submit_llm_votes(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        candidates: Sequence[int] | None,
        round_: int,
    ) -> None: ...
    async def submit_llm_daystart_speeches(
        self, game: Game, players: Sequence[Player], seats: Sequence[Seat]
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
    ) -> None:
        self.repo = repo
        self.discord = discord
        self.llm = llm
        self.wake = wake
        self.clock = clock
        self.rng = rng or Random()
        self._advance_locks: dict[str, asyncio.Lock] = {}

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

        # 3. Public announcements (including morning) and private logs.
        for entry in transition.public_logs:
            # MORNING entries are persisted via public_logs but rendered to Discord
            # through post_morning() below (with ☀️ decoration). Skip here to avoid
            # double-posting. See state_machine.plan_night_resolve — kind="MORNING".
            if entry.kind == "MORNING":
                continue
            await self._safe_post_public(new_game, entry.text, entry.kind)
        for entry in transition.private_logs:
            if entry.audience_seat is None:
                continue
            await self._safe_send_private(new_game, entry.audience_seat, entry.text, entry.kind)
        if transition.morning_text is not None:
            await self._safe_post_morning(new_game, transition.morning_text)

        # 4. Announce WAITING status if we paused.
        if transition.requires_host_decision and transition.pending is not None:
            try:
                await self.discord.announce_waiting(new_game, transition.pending, seats)
            except Exception:
                log.exception("waiting announce failed for %s", game_id)

        # 5. On entering DAY_VOTE / DAY_RUNOFF / NIGHT, kick off DMs.
        await self._dispatch_submissions(new_game, players, seats, transition)

        # 6. Victory handling: end game.
        if transition.victory is not None:
            try:
                await self.discord.on_game_end(new_game, seats)
            except Exception:
                log.exception("on_game_end failed for %s", game_id)
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
            return plan_day_discussion_to_vote(game, now)
        if game.phase is Phase.DAY_VOTE:
            votes = await self.repo.load_votes(game.id, day=game.day_number, round_=0)
            return plan_day_vote_resolve(game, players, seats, votes, game.force_skip_pending, now)
        if game.phase is Phase.DAY_RUNOFF:
            round0 = await self.repo.load_votes(game.id, day=game.day_number, round_=0)
            alive = {p.seat_no for p in players if p.alive}
            tied = compute_vote_result(round0, alive).tied
            round1 = await self.repo.load_votes(game.id, day=game.day_number, round_=1)
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
            prev_seat = prev[1] if prev else None
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
                await self.discord.send_night_action_dms(new_game, alive_players, seats)
            except Exception:
                log.exception("send_night_action_dms failed for %s", new_game.id)
            try:
                await self.llm.submit_llm_night_actions(new_game, alive_players, seats)
            except Exception:
                log.exception("llm night action submission failed")
        elif transition.next_phase is Phase.DAY_DISCUSSION:
            alive_players = [p for p in players_after if p.alive]
            try:
                await self.llm.submit_llm_daystart_speeches(new_game, alive_players, seats)
            except Exception:
                log.exception("llm day-start speeches failed")

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
        if target_seat is not None:
            if kind is SubmissionType.WOLF_ATTACK:
                legal = legal_attack_targets(players, actor_seat)
            elif kind is SubmissionType.SEER_DIVINE:
                legal = legal_divine_targets(players, actor_seat)
            else:  # KNIGHT_GUARD
                prev = await self.repo.load_previous_guard(game_id)
                prev_target = prev[1] if prev is not None else None
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
        """Re-send DM UIs to humans whose submission we're still waiting on.

        Called from recovery after engine reattach, and from `host_extend` when
        a WAITING phase is resumed. Needed because VoteView/NightActionView
        hold their submit callback in an in-memory closure that does not
        survive a bot restart, and because `/wolf extend` does not trigger a
        phase-entry Transition (so `_dispatch_submissions` never re-fires).

        No-ops for phases that never had DM UIs (LOBBY/SETUP/NIGHT_0/
        DAY_DISCUSSION/WAITING_HOST_DECISION/GAME_OVER). `send_vote_dms` /
        `send_night_action_dms` already filter LLM seats internally, so only
        human players receive the resend.

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
            else:
                round0 = await self.repo.load_votes(game_id, day=game.day_number, round_=0)
                tied = set(compute_vote_result(round0, alive_seats).tied)
                candidates = [s for s in seats if s.seat_no in tied]
            try:
                await self.discord.send_vote_dms(game, voters, candidates, round_=round_)
            except Exception:
                log.exception("resend_pending_dms vote failed for %s", game_id)
            return

        # NIGHT
        unresolved_by_kind = await unresolved_submitters(self.repo, game, players)
        seats_to_dm: set[int] = set()
        for seats_tuple in missing_by_kind.values():
            seats_to_dm.update(seats_tuple)
        for seats_tuple in unresolved_by_kind.values():
            seats_to_dm.update(seats_tuple)
        if not seats_to_dm:
            return
        actors = [p for p in players if p.seat_no in seats_to_dm]
        try:
            await self.discord.send_night_action_dms(game, actors, seats)
        except Exception:
            log.exception("resend_pending_dms night failed for %s", game_id)

    async def _all_votes_in(self, game: Game, round_: int) -> bool:
        players = await self.repo.load_players(game.id)
        votes = await self.repo.load_votes(game.id, day=game.day_number, round_=round_)
        alive = {p.seat_no for p in players if p.alive}
        submitted = {v.voter_seat for v in votes if v.voter_seat in alive}
        return alive.issubset(submitted)

    async def _all_night_actions_in(self, game: Game) -> bool:
        players = await self.repo.load_players(game.id)
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
        return expected.issubset(got)

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
