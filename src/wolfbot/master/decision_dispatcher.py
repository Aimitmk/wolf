"""Master-side fan-out of vote / night-action decision requests to NPC bots.

Phase-D: in `reactive_voice` mode each NPC bot is the embodied agent for
its seat and decides votes / night actions via its own `NPC_LLM_*`. This
dispatcher:

1. Builds a `DecideVoteRequest` (or `DecideNightActionRequest`) per LLM
   seat, sends it over WS, and parks an `asyncio.Future` per
   `request_id`.
2. Resolves the matching future when `VoteDecision` /
   `NightActionDecision` arrives on the WS handler chain.
3. Hits a deadline → resolves the future with ``target_seat=None``
   (= abstain / skip), logs the timeout, and the seat appears as a
   no-decision row in the viewer (per the user's "log it so the viewer
   shows the seat went silent" rule).

The dispatcher is decoupled from `LLMAdapter` and `SpeakArbiter` so the
rounds-mode gameplay-LLM path stays untouched. `LLMAdapter.submit_llm_*`
methods branch on `game.discussion_mode` and delegate here for
reactive_voice.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from wolfbot.domain.discussion import make_phase_id
from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Player, Seat
from wolfbot.domain.ws_messages import (
    DecideNightActionRequest,
    DecideVoteRequest,
    NightActionDecision,
    VoteDecision,
)
from wolfbot.master.npc_registry import NpcEntry, NpcRegistry

log = logging.getLogger(__name__)


@dataclass
class DecisionDispatcherConfig:
    """TTLs are deliberately conservative — the NPC bot pipeline is:

    register → snapshot in memory → LLM call (~2-4s with reasoning off)
    → reply over WS. A typical decision round-trips in well under 10s
    even at the bad tail; we set 12s so a slow provider still resolves
    before the deadline.
    """

    request_ttl_ms: int = 12_000


@dataclass
class _PendingDecision:
    """Per-request bookkeeping for the response side of the WS round-trip."""

    future: asyncio.Future[int | None]
    seat_no: int
    npc_id: str
    request_id: str


class NpcDecisionDispatcher:
    """Send DecideVoteRequest / DecideNightActionRequest to NPC bots and
    collect their decisions.

    Stateless across games — the only mutable state is `_pending`, which
    maps in-flight `request_id` → future. The same dispatcher instance
    serves every game on the Master process.
    """

    def __init__(
        self,
        registry: NpcRegistry,
        *,
        config: DecisionDispatcherConfig | None = None,
        now_ms: Callable[[], int] = lambda: 0,
    ) -> None:
        self.registry = registry
        self.config = config or DecisionDispatcherConfig()
        self._now_ms = now_ms
        # request_id → pending future. Cleaned up on resolution / timeout.
        self._pending: dict[str, _PendingDecision] = {}

    # ------------------------------------------------- public dispatch entry

    async def dispatch_votes(
        self,
        *,
        game_id: str,
        day: int,
        round_: int,
        voters: Sequence[Player],
        seats: Sequence[Seat],
        candidate_seats: Sequence[int],
        public_state_summary: str = "",
    ) -> dict[int, int | None]:
        """Fan out vote requests to every alive LLM voter; collect results.

        Returns ``{voter_seat_no: target_seat_or_None}``. A voter whose
        NPC is offline / never replies before the deadline maps to
        ``None`` (abstain).
        """
        seats_by_no = {s.seat_no: s for s in seats}
        candidate_pairs = tuple(
            (no, seats_by_no[no].display_name)
            for no in candidate_seats
            if no in seats_by_no
        )
        phase = Phase.DAY_RUNOFF if round_ >= 1 else Phase.DAY_VOTE
        phase_id = make_phase_id(game_id, day, phase)
        deadline = self._now_ms() + self.config.request_ttl_ms

        async def _one(voter: Player) -> tuple[int, int | None]:
            target = await self._dispatch_one_vote(
                voter=voter,
                seats_by_no=seats_by_no,
                candidate_pairs=candidate_pairs,
                game_id=game_id,
                phase_id=phase_id,
                round_=round_,
                expires_at_ms=deadline,
                public_state_summary=public_state_summary,
            )
            return voter.seat_no, target

        results = await asyncio.gather(
            *(_one(v) for v in voters), return_exceptions=False
        )
        return dict(results)

    async def dispatch_night_actions(
        self,
        *,
        game_id: str,
        day: int,
        action_kind: str,
        actors: Sequence[Player],
        seats: Sequence[Seat],
        candidate_seats: Sequence[int],
        public_state_summary: str = "",
    ) -> dict[int, int | None]:
        """Fan out night-action requests; collect targets per actor seat.

        ``action_kind`` is one of ``wolf_attack`` / ``seer_divine`` /
        ``knight_guard``. Master is responsible for selecting the right
        ``actors`` (e.g. only alive wolves for wolf_attack) and the
        legal ``candidate_seats`` set.
        """
        seats_by_no = {s.seat_no: s for s in seats}
        candidate_pairs = tuple(
            (no, seats_by_no[no].display_name)
            for no in candidate_seats
            if no in seats_by_no
        )
        phase_id = make_phase_id(
            game_id, day, Phase.NIGHT_0 if day == 0 else Phase.NIGHT
        )
        deadline = self._now_ms() + self.config.request_ttl_ms

        async def _one(actor: Player) -> tuple[int, int | None]:
            target = await self._dispatch_one_night(
                actor=actor,
                seats_by_no=seats_by_no,
                candidate_pairs=candidate_pairs,
                game_id=game_id,
                phase_id=phase_id,
                action_kind=action_kind,
                expires_at_ms=deadline,
                public_state_summary=public_state_summary,
            )
            return actor.seat_no, target

        results = await asyncio.gather(
            *(_one(a) for a in actors), return_exceptions=False
        )
        return dict(results)

    # ------------------------------------------------- WS handler hooks

    async def on_vote_decision(self, msg: VoteDecision) -> None:
        """Resolve the pending future for ``msg.request_id``. Stale or
        unknown ids are dropped silently — they were already timed out."""
        pending = self._pending.pop(msg.request_id, None)
        if pending is None:
            log.info(
                "vote_decision_unknown_request request=%s npc=%s",
                msg.request_id, msg.npc_id,
            )
            return
        if not pending.future.done():
            pending.future.set_result(msg.target_seat)
        log.info(
            "vote_decision_received request=%s npc=%s seat=%d target=%s reason=%s",
            msg.request_id, msg.npc_id, msg.seat_no, msg.target_seat,
            msg.reason_summary or "(none)",
        )

    async def on_night_action_decision(self, msg: NightActionDecision) -> None:
        pending = self._pending.pop(msg.request_id, None)
        if pending is None:
            log.info(
                "night_action_decision_unknown_request request=%s npc=%s",
                msg.request_id, msg.npc_id,
            )
            return
        if not pending.future.done():
            pending.future.set_result(msg.target_seat)
        log.info(
            "night_action_decision_received request=%s npc=%s seat=%d "
            "kind=%s target=%s reason=%s",
            msg.request_id, msg.npc_id, msg.seat_no, msg.action_kind,
            msg.target_seat, msg.reason_summary or "(none)",
        )

    # ------------------------------------------------- internals

    async def _dispatch_one_vote(
        self,
        *,
        voter: Player,
        seats_by_no: dict[int, Seat],
        candidate_pairs: tuple[tuple[int, str], ...],
        game_id: str,
        phase_id: str,
        round_: int,
        expires_at_ms: int,
        public_state_summary: str,
    ) -> int | None:
        seat = seats_by_no.get(voter.seat_no)
        if seat is None or not seat.is_llm:
            return None
        entry = self._find_npc_for_seat(game_id, voter.seat_no)
        if entry is None or entry.send is None:
            log.info(
                "vote_dispatch_skip_no_npc game=%s seat=%d", game_id, voter.seat_no
            )
            return None
        request_id = f"rv_{uuid.uuid4().hex[:12]}"
        req = DecideVoteRequest(
            ts=self._now_ms(),
            trace_id=f"vote-{game_id}-{voter.seat_no}-r{round_}",
            request_id=request_id,
            npc_id=entry.npc_id,
            seat_no=voter.seat_no,
            game_id=game_id,
            phase_id=phase_id,
            round_=round_,
            candidate_seats=candidate_pairs,
            public_state_summary=public_state_summary,
            expires_at_ms=expires_at_ms,
        )
        return await self._send_and_wait(
            request_id=request_id,
            seat_no=voter.seat_no,
            npc_id=entry.npc_id,
            send=entry.send,
            payload_json=req.model_dump_json(),
            label="vote",
        )

    async def _dispatch_one_night(
        self,
        *,
        actor: Player,
        seats_by_no: dict[int, Seat],
        candidate_pairs: tuple[tuple[int, str], ...],
        game_id: str,
        phase_id: str,
        action_kind: str,
        expires_at_ms: int,
        public_state_summary: str,
    ) -> int | None:
        seat = seats_by_no.get(actor.seat_no)
        if seat is None or not seat.is_llm:
            return None
        entry = self._find_npc_for_seat(game_id, actor.seat_no)
        if entry is None or entry.send is None:
            log.info(
                "night_dispatch_skip_no_npc game=%s seat=%d kind=%s",
                game_id, actor.seat_no, action_kind,
            )
            return None
        # Tighten the kind to the wire-level Literal — the only callers come
        # from rule-validated state machine code, so anything else is a bug.
        if action_kind not in ("wolf_attack", "seer_divine", "knight_guard"):
            log.warning(
                "night_dispatch_unknown_kind game=%s seat=%d kind=%s",
                game_id, actor.seat_no, action_kind,
            )
            return None
        request_id = f"rn_{uuid.uuid4().hex[:12]}"
        req = DecideNightActionRequest(
            ts=self._now_ms(),
            trace_id=f"night-{game_id}-{actor.seat_no}-{action_kind}",
            request_id=request_id,
            npc_id=entry.npc_id,
            seat_no=actor.seat_no,
            game_id=game_id,
            phase_id=phase_id,
            action_kind=action_kind,  # type: ignore[arg-type]
            candidate_seats=candidate_pairs,
            public_state_summary=public_state_summary,
            expires_at_ms=expires_at_ms,
        )
        return await self._send_and_wait(
            request_id=request_id,
            seat_no=actor.seat_no,
            npc_id=entry.npc_id,
            send=entry.send,
            payload_json=req.model_dump_json(),
            label=f"night-{action_kind}",
        )

    async def _send_and_wait(
        self,
        *,
        request_id: str,
        seat_no: int,
        npc_id: str,
        send: Callable[[str], Awaitable[None]],
        payload_json: str,
        label: str,
    ) -> int | None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[int | None] = loop.create_future()
        self._pending[request_id] = _PendingDecision(
            future=future, seat_no=seat_no, npc_id=npc_id, request_id=request_id,
        )
        try:
            await send(payload_json)
        except Exception:
            log.exception(
                "%s_dispatch_send_failed npc=%s seat=%d", label, npc_id, seat_no,
            )
            self._pending.pop(request_id, None)
            return None
        timeout_s = self.config.request_ttl_ms / 1000.0
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError:
            log.info(
                "%s_dispatch_timeout npc=%s seat=%d request=%s",
                label, npc_id, seat_no, request_id,
            )
            return None
        finally:
            self._pending.pop(request_id, None)

    def _find_npc_for_seat(self, game_id: str, seat_no: int) -> NpcEntry | None:
        for entry in self.registry.all_online():
            if entry.assigned_seat == seat_no and entry.game_id == game_id:
                return entry
        return None


__all__ = ["DecisionDispatcherConfig", "NpcDecisionDispatcher"]


# Avoid unused-import lint: Role is consumed by callers that pass `actors`
# pre-filtered by role; we keep the import handy for future role-aware
# dispatch policies without forcing the call site to import it again.
_ = Role
