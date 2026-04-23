"""xAI (Grok) LLM service + LLMAdapter that drives LLM players.

- `LLMAction`: the Pydantic shape returned by the model (same schema sent via
  response_format).
- `LLMActionDecider` Protocol: low-level "given a persona+context, return an LLMAction".
- `XAILLMActionDecider`: calls xAI's OpenAI-compat endpoint with structured output.
- `FakeLLMActionDecider`: deterministic stub for tests/offline dry runs.
- `LLMAdapter`: implements the LLMAdapter Protocol consumed by game_service; iterates
  LLM seats and submits their actions via GameService.submit_*.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import Game, Player, Seat
from wolfbot.domain.rules import (
    legal_attack_targets,
    legal_divine_targets,
    legal_guard_targets,
)
from wolfbot.llm.personas import PERSONAS_BY_KEY
from wolfbot.llm.prompt_builder import (
    build_system_prompt,
    build_user_context,
    task_daytime_speech,
    task_night_action,
    task_vote,
)

if TYPE_CHECKING:  # avoid importing heavy modules unless needed
    from openai import AsyncOpenAI

    from wolfbot.persistence.sqlite_repo import SqliteRepo
    from wolfbot.services.game_service import GameService


class MessagePoster(Protocol):
    """Subset of DiscordBotAdapter's public-post API; decoupled for testing."""

    async def post_public(self, game: Game, text: str, kind: str) -> None: ...


log = logging.getLogger(__name__)


# ---------------------------------------------------------------- LLMAction
class LLMAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent: Literal["speak", "vote", "night_action", "skip"]
    public_message: str = ""
    target_name: str | None = None
    reason_summary: str = ""
    confidence: float = 0.5


RESPONSE_SCHEMA: dict[str, object] = {
    "name": "wolfbot_action",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "intent",
            "public_message",
            "target_name",
            "reason_summary",
            "confidence",
        ],
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["speak", "vote", "night_action", "skip"],
            },
            "public_message": {"type": "string", "maxLength": 400},
            "target_name": {"type": ["string", "null"]},
            "reason_summary": {"type": "string", "maxLength": 200},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    },
}


# ---------------------------------------------------------- low-level deciders
class LLMActionDecider(Protocol):
    async def decide(self, system_prompt: str, user_context: str) -> LLMAction: ...


class XAILLMActionDecider:
    """Calls xAI's OpenAI-compatible chat completions endpoint."""

    def __init__(self, client: AsyncOpenAI, model: str, timeout: float = 30.0) -> None:
        self.client = client
        self.model = model
        self.timeout = timeout

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        # xAI model IDs aren't in the openai SDK's Literal, hence the ignore.
        resp = await self.client.chat.completions.create(  # type: ignore[call-overload]
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_context},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": RESPONSE_SCHEMA,
            },
            timeout=self.timeout,
        )
        message = resp.choices[0].message
        content = message.content or "{}"
        return LLMAction.model_validate_json(content)


class FakeLLMActionDecider:
    """Deterministic stub. Returns ACTIONS[n] round-robin per call."""

    def __init__(
        self,
        scripted: Sequence[LLMAction] | None = None,
        default: LLMAction | None = None,
    ) -> None:
        self._scripted: list[LLMAction] = list(scripted or [])
        self._default = default or LLMAction(intent="skip", reason_summary="fake-default")
        self.call_count = 0

    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        self.call_count += 1
        if self._scripted:
            return self._scripted.pop(0)
        return self._default


# ---------------------------------------------------------------- LLMAdapter
class LLMAdapter:
    """Iterates LLM players and submits decisions via GameService.

    The returned `target_name` must match one of the legal candidate display names; on
    mismatch we try one re-prompt (via the decider's own retry path), then fall back to
    a uniform-random legal target (logged as a warning). For nullable intent, we call
    `submit_*` with target_seat=None (abstain / no-action).
    """

    NORMAL_SPEECH_CAP = 3
    COOLDOWN_SECONDS = 20
    REACTION_KEYWORDS = ("CO", "占い", "霊媒", "白", "黒", "疑", "カミングアウト")

    def __init__(
        self,
        repo: SqliteRepo,
        decider: LLMActionDecider,
        message_poster: MessagePoster | None = None,
        game_service_ref: dict[str, GameService] | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        import time as _time

        self.repo = repo
        self.decider = decider
        self.message_poster = message_poster
        self._gs_slot: dict[str, GameService] = game_service_ref or {}
        self.rng = rng or random.Random()
        self._clock: Callable[[], int] = clock or (lambda: int(_time.time()))
        self._background_tasks: set[asyncio.Task[None]] = set()

    def set_game_service(self, gs: GameService) -> None:
        self._gs_slot["gs"] = gs

    @property
    def gs(self) -> GameService:
        gs = self._gs_slot.get("gs")
        if gs is None:
            raise RuntimeError("LLMAdapter.set_game_service(...) was not called")
        return gs

    # ------------------------------------------------------ night actions
    async def submit_llm_night_actions(
        self, game: Game, players: Sequence[Player], seats: Sequence[Seat]
    ) -> None:
        seats_by_no = {s.seat_no: s for s in seats}
        llm_players = [
            p
            for p in players
            if p.alive and seats_by_no.get(p.seat_no) is not None and seats_by_no[p.seat_no].is_llm
        ]
        prev = await self.repo.load_previous_guard(game.id)
        prev_guard_seat = prev[1] if prev else None

        for player in llm_players:
            seat = seats_by_no[player.seat_no]
            if player.role is None or seat.persona_key is None:
                continue
            kind, legal = self._role_to_kind(player, players, prev_guard_seat)
            if kind is None or not legal:
                continue
            candidates = [seats_by_no[sn] for sn in legal if sn in seats_by_no]
            action = await self._ask(
                game,
                player,
                seat,
                players,
                seats,
                task_text=task_night_action(kind, [c.display_name for c in candidates]),
            )
            target_seat = self._resolve_target(action.target_name, candidates, allow_none=False)
            await self.gs.submit_night_action(game.id, player.seat_no, kind, target_seat)

    # ------------------------------------------------------ votes
    async def submit_llm_votes(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        candidates: Sequence[int] | None,
        round_: int,
    ) -> None:
        seats_by_no = {s.seat_no: s for s in seats}
        llm_voters = [
            p
            for p in players
            if p.alive and p.seat_no in seats_by_no and seats_by_no[p.seat_no].is_llm
        ]
        for voter in llm_voters:
            seat = seats_by_no[voter.seat_no]
            if seat.persona_key is None:
                continue
            if candidates is None:
                cand_seats = [
                    s
                    for s in seats
                    if s.seat_no != voter.seat_no
                    and any(p.seat_no == s.seat_no and p.alive for p in players)
                ]
            else:
                cand_seats = [
                    s for s in seats if s.seat_no in set(candidates) and s.seat_no != voter.seat_no
                ]
            if not cand_seats:
                await self.gs.submit_vote(game.id, voter.seat_no, target_seat=None, round_=round_)
                continue
            action = await self._ask(
                game,
                voter,
                seat,
                players,
                seats,
                task_text=task_vote([c.display_name for c in cand_seats], runoff=round_ == 1),
            )
            target = self._resolve_target(
                action.target_name, cand_seats, allow_none=action.intent == "skip"
            )
            await self.gs.submit_vote(game.id, voter.seat_no, target_seat=target, round_=round_)

    # --------------------------------------------------- daytime speeches
    async def submit_llm_daystart_speeches(
        self, game: Game, players: Sequence[Player], seats: Sequence[Seat]
    ) -> None:
        """Schedule each alive LLM's first speech of the day, sequentially with jitter.

        Fire-and-forget — the caller (game_service.advance) doesn't wait.
        """
        seats_by_no = {s.seat_no: s for s in seats}
        llm_players = [
            p
            for p in players
            if p.alive and p.seat_no in seats_by_no and seats_by_no[p.seat_no].is_llm
        ]
        if not llm_players:
            return
        task = asyncio.create_task(
            self._run_daystart(game, llm_players, seats),
            name=f"llm-daystart-{game.id}-{game.day_number}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_daystart(
        self,
        game: Game,
        llm_players: Sequence[Player],
        seats: Sequence[Seat],
    ) -> None:
        seats_by_no = {s.seat_no: s for s in seats}
        for llm in llm_players:
            # Re-read the live phase before each speech. The loop sleeps up to
            # 10s between LLM turns; by the time we get here the game may have
            # moved on (discussion ended, recovery swapped phases, etc.) and
            # posting a "daystart" line would be noise.
            fresh = await self.repo.load_game(game.id)
            if (
                fresh is None
                or fresh.phase is not Phase.DAY_DISCUSSION
                or fresh.day_number != game.day_number
            ):
                return
            seat = seats_by_no.get(llm.seat_no)
            if seat is None:
                continue
            await self._maybe_speak(fresh, llm, seat, seats)
            try:
                await asyncio.sleep(self.rng.uniform(3, 10))
            except asyncio.CancelledError:
                return

    async def maybe_react_to_message(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        author_seat: int | None,
        text: str,
    ) -> None:
        """Called on every main-text human message. Alive LLMs may react."""
        if game.phase is not Phase.DAY_DISCUSSION:
            return
        seats_by_no = {s.seat_no: s for s in seats}
        for llm in players:
            if not llm.alive:
                continue
            seat = seats_by_no.get(llm.seat_no)
            if seat is None or not seat.is_llm:
                continue
            if author_seat == llm.seat_no:
                continue
            if not self._is_triggered(seat, text):
                continue
            # Check cap + cooldown before even calling the LLM
            count, _, last = await self.repo.load_llm_speech(game.id, game.day_number, llm.seat_no)
            now = self._clock()
            if count >= self.NORMAL_SPEECH_CAP:
                continue
            if last is not None and now - last < self.COOLDOWN_SECONDS:
                continue
            await self._maybe_speak(game, llm, seat, seats)

    def _is_triggered(self, seat: Seat, text: str) -> bool:
        if seat.display_name and seat.display_name in text:
            return True
        if seat.persona_key and seat.persona_key.lower() in text.lower():
            return True
        return any(kw in text for kw in self.REACTION_KEYWORDS)

    async def _maybe_speak(
        self,
        game: Game,
        player: Player,
        seat: Seat,
        seats: Sequence[Seat],
    ) -> None:
        """Ask the LLM; if intent=speak, post to main and increment the count."""
        count, _, last = await self.repo.load_llm_speech(game.id, game.day_number, player.seat_no)
        if count >= self.NORMAL_SPEECH_CAP:
            return
        now = self._clock()
        if last is not None and now - last < self.COOLDOWN_SECONDS:
            return
        players = await self.repo.load_players(game.id)
        action = await self._ask(
            game,
            player,
            seat,
            players,
            seats,
            task_text=task_daytime_speech(game.day_number),
        )
        if action.intent != "speak":
            return
        message = action.public_message.strip()
        if not message:
            return
        # Belt-and-suspenders: the LLM call itself can take seconds. Re-check
        # phase right before posting so we don't dump a stale speech into a
        # channel that has already moved on to voting or night.
        fresh = await self.repo.load_game(game.id)
        if (
            fresh is None
            or fresh.phase is not Phase.DAY_DISCUSSION
            or fresh.day_number != game.day_number
        ):
            return
        if self.message_poster is not None:
            try:
                await self.message_poster.post_public(
                    fresh, f"**{seat.display_name}**: {message}", kind="LLM_SPEAK"
                )
            except Exception:
                log.exception("post_public for LLM speech failed")
        await self.repo.increment_llm_normal_speech(game.id, game.day_number, player.seat_no, now)

    # ------------------------------------------------------ helpers
    async def _ask(
        self,
        game: Game,
        player: Player,
        seat: Seat,
        players: Sequence[Player],
        seats: Sequence[Seat],
        task_text: str,
    ) -> LLMAction:
        persona = PERSONAS_BY_KEY.get(seat.persona_key or "")
        if persona is None:
            return LLMAction(intent="skip", reason_summary="persona missing")
        assert player.role is not None
        public_logs = await self.repo.load_public_logs(game.id, limit=40)
        private_logs = await self.repo.load_private_logs_for_audience(
            game.id, audience_seat=player.seat_no, limit=40
        )
        system = build_system_prompt(
            persona=persona,
            role=player.role,
            phase=game.phase,
            day_number=game.day_number,
            task_text=task_text,
        )
        user = build_user_context(
            game=game,
            me=player,
            my_seat=seat,
            seats=seats,
            players=players,
            public_logs=public_logs,
            private_logs=private_logs,
        )
        try:
            return await self.decider.decide(system, user)
        except Exception:
            log.exception("LLM decide failed for seat %s game %s", player.seat_no, game.id)
            return LLMAction(intent="skip", reason_summary="decider error")

    def _resolve_target(
        self,
        target_name: str | None,
        candidates: Sequence[Seat],
        *,
        allow_none: bool,
    ) -> int | None:
        if target_name is None:
            if allow_none:
                return None
            pick = self.rng.choice(list(candidates))
            log.warning("LLM returned null target; fallback to %s", pick.display_name)
            return pick.seat_no
        for c in candidates:
            if c.display_name == target_name:
                return c.seat_no
        log.warning("LLM target_name %r not in candidates; random fallback", target_name)
        return self.rng.choice(list(candidates)).seat_no

    @staticmethod
    def _role_to_kind(
        player: Player,
        all_players: Sequence[Player],
        prev_guard_seat: int | None,
    ) -> tuple[SubmissionType | None, list[int]]:
        if player.role is Role.WEREWOLF:
            return (
                SubmissionType.WOLF_ATTACK,
                legal_attack_targets(all_players, player.seat_no),
            )
        if player.role is Role.SEER:
            return (
                SubmissionType.SEER_DIVINE,
                legal_divine_targets(all_players, player.seat_no),
            )
        if player.role is Role.KNIGHT:
            return (
                SubmissionType.KNIGHT_GUARD,
                legal_guard_targets(all_players, player.seat_no, prev_guard_seat),
            )
        return (None, [])


# ------------------------------------------------------------- factory
def make_xai_decider(api_key: str, model: str, timeout: float = 30.0) -> XAILLMActionDecider:
    """Build an xAI-backed decider. Imports openai lazily so tests can skip it."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    return XAILLMActionDecider(client=client, model=model, timeout=timeout)


__all__ = [
    "RESPONSE_SCHEMA",
    "FakeLLMActionDecider",
    "LLMAction",
    "LLMActionDecider",
    "LLMAdapter",
    "XAILLMActionDecider",
    "make_xai_decider",
]

# keep Phase referenced for mypy
_ = Phase
