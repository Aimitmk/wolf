"""LLM service + LLMAdapter that drives LLM players (xAI Grok, DeepSeek, or Gemini).

- `LLMAction`: the Pydantic shape returned by the model (same schema sent via
  response_format on the xAI path; via response_json_schema on the Gemini path;
  checked post-hoc on the DeepSeek path).
- `LLMActionDecider` Protocol: low-level "given a persona+context, return an LLMAction".
- `XAILLMActionDecider`: calls xAI's OpenAI-compat endpoint with json_schema strict mode.
- `DeepSeekLLMActionDecider`: calls DeepSeek's OpenAI-compat endpoint with json_object
  + appended JSON contract; thinking mode and reasoning_effort are configurable.
- `GeminiLLMActionDecider`: calls Vertex AI's Gemini API via the official
  google-genai SDK with response_json_schema structured output and configurable
  thinking_level. Authenticates via ADC/IAM (no API key); Vertex AI Express mode
  is deliberately unsupported.
- `FakeLLMActionDecider`: deterministic stub for tests/offline dry runs.
- `make_llm_decider(cfg)`: provider-aware factory; branches on
  `LLMDeciderConfig.provider`.  ``cfg`` is built by
  ``MasterSettings.gameplay_decider_config()`` from ``GAMEPLAY_LLM_*``
  env vars (the symmetrical NPC factory in
  :mod:`wolfbot.npc.generator_factory` consumes the same config dataclass
  built from ``NPC_LLM_*`` instead).
- `LLMAdapter`: implements the LLMAdapter Protocol consumed by game_service; iterates
  LLM seats and submits their actions via GameService.submit_*.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from wolfbot.domain.discussion import CoClaim, make_phase_id
from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import Game, LogEntry, Player, Seat, Vote
from wolfbot.domain.rules import (
    legal_attack_targets,
    legal_divine_targets,
    legal_guard_targets,
    previous_guard_seat_for_night,
)
from wolfbot.llm.decider_config import LLMDeciderConfig
from wolfbot.llm.prompt_builder import (
    build_system_prompt,
    build_user_context,
    task_daytime_speech,
    task_night_action,
    task_vote,
    task_wolf_chat,
)
from wolfbot.npc.personas import NPC_PERSONAS_BY_KEY
from wolfbot.services.llm_trace import (
    CallTimer,
    log_llm_call,
    trace_context,
)

if TYPE_CHECKING:  # avoid importing heavy modules unless needed
    from openai import AsyncOpenAI

    from wolfbot.persistence.sqlite_repo import SqliteRepo
    from wolfbot.services.discussion_service import DiscussionService
    from wolfbot.services.game_service import GameService

# Sleep ranges between consecutive LLM speeches inside DAY_DISCUSSION rounds.
# Sequential per round so each LLM reads the previous one's contribution.
DISCUSSION_INTRA_ROUND_DELAY: tuple[float, float] = (3.0, 10.0)
DISCUSSION_INTER_ROUND_DELAY: tuple[float, float] = (5.0, 15.0)


class MessagePoster(Protocol):
    """Subset of DiscordBotAdapter's public-post API; decoupled for testing."""

    async def post_public(self, game: Game, text: str, kind: str) -> None: ...
    async def post_wolves_chat(self, game: Game, text: str, kind: str) -> None: ...


log = logging.getLogger(__name__)


def _classify_task_text(task_text: str) -> str:
    """Coarse tag of which `task_*` produced this prompt — for trace metadata only."""
    if "投票先として合法な候補は" in task_text:
        return "vote"
    if "対象を 1 名選んでください" in task_text:
        return "night_action"
    if "人狼チャット" in task_text:
        return "wolf_chat"
    if "議論フェイズ" in task_text or "演説" in task_text:
        return "discussion"
    return "unknown"


def seat_token(seat: Seat) -> str:
    """Stable LLM/UI identifier: `席{N} {display_name}`.

    Disambiguates candidates when display_name collides (duplicate humans, or a
    human named the same as a persona). Resolver parses the leading `席\\d+`.
    """
    return f"席{seat.seat_no} {seat.display_name}"


_SEAT_TOKEN_RE = re.compile(r"^\s*席(\d+)\b")


# ---------------------------------------------------------------- LLMAction
class LLMAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent: Literal["speak", "vote", "night_action", "skip"]
    public_message: str = ""
    target_name: str | None = None
    reason_summary: str = ""
    confidence: float = 0.5
    co_declaration: Literal["seer", "medium", "knight"] | None = None


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
            "co_declaration",
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
            "co_declaration": {
                "type": ["string", "null"],
                "enum": ["seer", "medium", "knight", None],
            },
        },
    },
}


# DeepSeek's `json_object` mode requires the system prompt to literally mention
# "json" and works best with a concrete example. We append this contract to the
# system prompt for every DeepSeek decision; the contract complements (does not
# replace) the markdown template loaded by prompt_builder. xAI uses json_schema
# strict mode and does not need this. Module-level so tests can assert on
# substrings without instantiating AsyncOpenAI.
_DEEPSEEK_JSON_CONTRACT_SUFFIX = """\

---
出力形式 (json):
必ず次のキーを持つ JSON オブジェクトのみを返してください。前後にテキストや markdown コードフェンスを付けないでください。
- "intent": "speak" | "vote" | "night_action" | "skip"
- "public_message": string (最大 400 文字)
- "target_name": string または null
- "reason_summary": string (最大 200 文字)
- "confidence": number (0 から 1)

例:
{"intent": "speak", "public_message": "私は占い師です。", "target_name": null, "reason_summary": "CO 表明", "confidence": 0.7}
"""


def _deepseek_json_contract(system_prompt: str) -> str:
    """Append DeepSeek's JSON-mode contract to a system prompt."""
    return system_prompt + _DEEPSEEK_JSON_CONTRACT_SUFFIX


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
        timer = CallTimer()
        content = ""
        err: str | None = None
        try:
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
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await log_llm_call(
                role="gameplay",
                provider="xai",
                model=self.model,
                system_prompt=system_prompt,
                user_prompt=user_context,
                response=content if err is None else None,
                latency_ms=timer.elapsed_ms,
                error=err,
            )


class DeepSeekLLMActionDecider:
    """Calls DeepSeek's OpenAI-compatible chat completions endpoint.

    DeepSeek does not support `response_format=json_schema` strict mode, so we
    use `json_object` and rely on `LLMAction.model_validate_json` for the same
    Pydantic check the xAI path uses. The system prompt has the JSON contract
    appended at call time so the model knows the field names without us
    re-walking the schema.

    `reasoning_content` is intentionally never read or logged — only
    `message.content` (the public answer) is consumed.
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        thinking: Literal["enabled", "disabled"] = "enabled",
        reasoning_effort: Literal["high", "max"] = "max",
        timeout: float = 30.0,
    ) -> None:
        self.client = client
        self.model = model
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        full_system = _deepseek_json_contract(system_prompt)
        kwargs: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": full_system},
                {"role": "user", "content": user_context},
            ],
            "response_format": {"type": "json_object"},
            "timeout": self.timeout,
            "extra_body": {"thinking": {"type": self.thinking}},
        }
        if self.thinking == "enabled":
            kwargs["reasoning_effort"] = self.reasoning_effort
        timer = CallTimer()
        content = ""
        err: str | None = None
        try:
            # DeepSeek model IDs aren't in the openai SDK's Literal, hence the ignore.
            resp = await self.client.chat.completions.create(**kwargs)  # type: ignore[call-overload]
            message = resp.choices[0].message
            content = message.content or "{}"
            return LLMAction.model_validate_json(content)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await log_llm_call(
                role="gameplay",
                provider="deepseek",
                model=self.model,
                system_prompt=full_system,
                user_prompt=user_context,
                response=content if err is None else None,
                latency_ms=timer.elapsed_ms,
                error=err,
                extra={
                    "thinking": self.thinking,
                    "reasoning_effort": self.reasoning_effort
                    if self.thinking == "enabled"
                    else None,
                },
            )


class GeminiLLMActionDecider:
    """Calls Vertex AI's Gemini API through the official google-genai SDK.

    Gemini 3 supports structured outputs via `response_json_schema` plus a
    configurable `thinking_level` (`minimal` / `low` / `medium` / `high`).
    Internal thinking and thought signatures are deliberately ignored — only
    `resp.text` is consumed, mirroring how the DeepSeek path treats
    `reasoning_content`.
    """

    def __init__(
        self,
        client: object,
        model: str,
        thinking_level: Literal["minimal", "low", "medium", "high"] = "high",
        timeout: float = 30.0,
    ) -> None:
        self.client = client
        self.model = model
        self.thinking_level = thinking_level
        self.timeout = timeout

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        from google.genai import types

        timer = CallTimer()
        content = ""
        err: str | None = None
        try:
            resp = await self.client.aio.models.generate_content(  # type: ignore[attr-defined]
                model=self.model,
                contents=user_context,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_json_schema=RESPONSE_SCHEMA["schema"],
                    thinking_config=types.ThinkingConfig(
                        # SDK normalizes the string into ThinkingLevel at runtime;
                        # the type annotation is enum-only, so silence the check.
                        thinking_level=self.thinking_level,  # type: ignore[arg-type]
                    ),
                ),
            )
            content = resp.text or "{}"
            return LLMAction.model_validate_json(content)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await log_llm_call(
                role="gameplay",
                provider="gemini",
                model=self.model,
                system_prompt=system_prompt,
                user_prompt=user_context,
                response=content if err is None else None,
                latency_ms=timer.elapsed_ms,
                error=err,
                extra={"thinking_level": self.thinking_level},
            )


class MockLLMActionDecider:
    """Offline mock decider used when ``GAMEPLAY_LLM_PROVIDER=mock``.

    Returns deterministic LLMActions based on phrases unique to each
    ``task_*`` prompt generator in :mod:`wolfbot.llm.prompt_builder`.
    Vote and night-action targets are intentionally ``None`` so
    :meth:`LLMAdapter._resolve_target` falls back to a uniform-random
    legal target — sidestepping the brittle "parse legal candidates out
    of user_context" path while still producing valid game progression.

    Speech messages are persona-blind canned phrases drawn round-robin
    from a per-instance counter, kept under 80 chars to fit the discussion
    text length convention. The Master Gameplay LLM only generates speech
    in ``rounds`` mode; in ``reactive_voice`` mode the speech path is
    routed to NPC bots and this decider produces only votes / night
    actions / wolf-chat coordination text.
    """

    _SPEECHES: tuple[str, ...] = (
        "状況を整理しましょうか。今のところ大きな決め手はないですね。",
        "発言の少ない方が気になります。考えを聞きたいです。",
        "占い結果が出るまで断定は避けたいですね。",
        "票筋から見ると、何人か怪しい位置はあります。",
        "今のところ私は様子を見たい立場です。",
    )

    _WOLF_CHAT: tuple[str, ...] = (
        "情報を持ってそうな位置を狙いたい。",
        "騎士候補から先に処理する案もある。",
        "今夜は無難な相手に揃えよう。",
    )

    def __init__(
        self,
        *,
        speeches: Sequence[str] | None = None,
        wolf_chat: Sequence[str] | None = None,
    ) -> None:
        self._speeches: tuple[str, ...] = tuple(speeches or self._SPEECHES)
        self._wolf_chat: tuple[str, ...] = tuple(wolf_chat or self._WOLF_CHAT)
        self._speech_idx = 0
        self._wolf_idx = 0
        self.call_count = 0

    def _next_speech(self) -> str:
        text = self._speeches[self._speech_idx % len(self._speeches)]
        self._speech_idx += 1
        return text

    def _next_wolf_chat(self) -> str:
        text = self._wolf_chat[self._wolf_idx % len(self._wolf_chat)]
        self._wolf_idx += 1
        return text

    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        self.call_count += 1
        # `task_text` is injected into the *system* prompt (see
        # `build_system_prompt`'s `{task_block}`), not the user context.
        # We scan both so the dispatch keys work regardless of where a
        # caller decides to put them.
        haystack = f"{system_prompt}\n{user_context}"
        if "投票先として合法な候補は" in haystack:
            return LLMAction(
                intent="vote",
                target_name=None,
                reason_summary="mock vote",
                confidence=0.5,
            )
        if "対象を 1 名選んでください" in haystack:
            # Deterministic target = smallest 席N appearing in the candidate
            # list. For WOLF_ATTACK both wolves see the *same* candidate list
            # (legal_attack_targets excludes all wolves), so they converge on
            # one target — avoiding the wolf-attack split that would otherwise
            # park the mock game in WAITING_HOST_DECISION every night.
            return LLMAction(
                intent="night_action",
                target_name=self._pick_smallest_seat_token(haystack),
                reason_summary="mock night action",
                confidence=0.5,
            )
        if "人狼チャット" in haystack:
            return LLMAction(
                intent="speak",
                public_message=self._next_wolf_chat(),
                reason_summary="mock wolf chat",
                confidence=0.5,
            )
        return LLMAction(
            intent="speak",
            public_message=self._next_speech(),
            reason_summary="mock speech",
            confidence=0.5,
        )

    @staticmethod
    def _pick_smallest_seat_token(prompt: str) -> str | None:
        # Parse only the "合法候補:" line — the rest of the prompt contains
        # an example token ("例: `席3 Alice`") that would otherwise pollute
        # the seat list and cause us to "pick" a non-candidate.
        match = re.search(r"合法候補:[ \t]*([^\n]*)", prompt)
        if match is None:
            return None
        seats = [int(m) for m in re.findall(r"席(\d+)", match.group(1))]
        if not seats:
            return None
        return f"席{min(seats)}"


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

    def __init__(
        self,
        repo: SqliteRepo,
        decider: LLMActionDecider,
        message_poster: MessagePoster | None = None,
        game_service_ref: dict[str, GameService] | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], int] | None = None,
        discussion_service: DiscussionService | None = None,
    ) -> None:
        import time as _time

        self.repo = repo
        self.decider = decider
        self.message_poster = message_poster
        self._gs_slot: dict[str, GameService] = game_service_ref or {}
        self.rng = rng or random.Random()
        self._clock: Callable[[], int] = clock or (lambda: int(_time.time()))
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Optional SpeechEvent emission. When None, behavior matches pre-
        # speech-event-bus rounds mode: only PLAYER_SPEECH log + Discord post
        # are produced. When set, every accepted utterance is also recorded
        # as SpeechEvent(source=npc_generated) so PublicDiscussionState can
        # rebuild a uniform timeline across rounds + reactive_voice modes.
        self.discussion_service = discussion_service

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
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        restrict_to_seats: frozenset[int] | None = None,
        unresolved_seats: frozenset[int] = frozenset(),
    ) -> None:
        """Schedule each LLM's night action in a background task.

        Fire-and-forget — the caller (game_service.advance) doesn't wait, so a
        slow xAI call can't block the GameEngine's deadline watcher.

        When called from `resend_pending_dms` (on `/wolf extend`), pass
        `restrict_to_seats` = union of missing + unresolved LLM seats so we only
        re-dispatch the ones that still owe a submission. `unresolved_seats`
        are those in a wolf-attack split — the in-loop "already submitted?"
        guard otherwise skips them, which would keep the split locked.
        """
        seats_by_no = {s.seat_no: s for s in seats}
        llm_players = [
            p
            for p in players
            if p.alive and seats_by_no.get(p.seat_no) is not None and seats_by_no[p.seat_no].is_llm
        ]
        if restrict_to_seats is not None:
            llm_players = [p for p in llm_players if p.seat_no in restrict_to_seats]
        if not llm_players:
            return
        task = asyncio.create_task(
            self._run_night_actions(game, llm_players, players, seats, unresolved_seats),
            name=f"llm-night-{game.id}-d{game.day_number}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_night_actions(
        self,
        game: Game,
        llm_players: Sequence[Player],
        all_players: Sequence[Player],
        seats: Sequence[Seat],
        unresolved_seats: frozenset[int] = frozenset(),
    ) -> None:
        seats_by_no = {s.seat_no: s for s in seats}
        prev = await self.repo.load_previous_guard(game.id)
        prev_guard_seat = previous_guard_seat_for_night(prev, game.day_number)

        # Wolf-chat prelude: before submitting attacks, LLM wolves post a
        # coordination message to the wolves-only channel so both wolves
        # converge on one target. Runs serially — wolf B sees wolf A's just-
        # logged message.
        await self._run_wolf_chat(game, llm_players, all_players, seats)

        async def _one_night_action(player: Player) -> None:
            # Per-seat stale check, idempotency, and submission. Wrapped in
            # try/except so one seat's failure does not cancel peers launched
            # alongside us in the gather below.
            try:
                fresh = await self.repo.load_game(game.id)
                if (
                    fresh is None
                    or fresh.ended_at is not None
                    or fresh.phase is not Phase.NIGHT
                    or fresh.day_number != game.day_number
                ):
                    return
                seat = seats_by_no[player.seat_no]
                if player.role is None or seat.persona_key is None:
                    return
                kind, legal = self._role_to_kind(player, all_players, prev_guard_seat)
                if kind is None or not legal:
                    return
                # Idempotency: skip if this seat already submitted for this
                # kind — unless it is a split-wolf (unresolved_seats), which
                # must re-ask to break the lockout.
                existing = await self.repo.load_night_actions(game.id, day=game.day_number)
                already = any(a.actor_seat == player.seat_no and a.kind is kind for a in existing)
                if already and player.seat_no not in unresolved_seats:
                    return
                candidates = [seats_by_no[sn] for sn in legal if sn in seats_by_no]
                action = await self._ask(
                    game,
                    player,
                    seat,
                    all_players,
                    seats,
                    task_text=task_night_action(kind, [seat_token(c) for c in candidates]),
                )
                target_seat = self._resolve_target(action.target_name, candidates, allow_none=False)
                await self.gs.submit_night_action(
                    game.id, player.seat_no, kind, target_seat, game.day_number
                )
            except Exception:
                log.exception(
                    "llm night action failed for game %s seat %s", game.id, player.seat_no
                )

        # Dispatch per-seat in parallel. xAI round-trips no longer stack
        # serially across LLM seats.
        await asyncio.gather(*(_one_night_action(p) for p in llm_players))

    async def _run_wolf_chat(
        self,
        game: Game,
        llm_players: Sequence[Player],
        all_players: Sequence[Player],
        seats: Sequence[Seat],
    ) -> None:
        """LLM wolves post a coordination message to the wolves-only channel.

        Runs at the start of NIGHT, before attack submissions, so each wolf's
        `task_night_action` sees the partner's proposed target in its WOLF_CHAT
        private log and can converge instead of split. Serial (wolf B reads
        wolf A's just-logged message). No-ops when the village has <2 alive
        wolves total, no LLM wolves alive, or no wolves channel configured.
        """
        if game.wolves_channel_id is None or self.message_poster is None:
            return
        seats_by_no = {s.seat_no: s for s in seats}
        alive_wolves_all = [p for p in all_players if p.alive and p.role is Role.WEREWOLF]
        if len(alive_wolves_all) < 2:
            return
        llm_wolves = [p for p in llm_players if p.role is Role.WEREWOLF]
        if not llm_wolves:
            return

        for wolf in llm_wolves:
            fresh = await self.repo.load_game(game.id)
            if (
                fresh is None
                or fresh.ended_at is not None
                or fresh.phase is not Phase.NIGHT
                or fresh.day_number != game.day_number
            ):
                return
            seat = seats_by_no.get(wolf.seat_no)
            if seat is None or seat.persona_key is None:
                continue
            legal = legal_attack_targets(all_players, wolf.seat_no)
            candidates = [seats_by_no[sn] for sn in legal if sn in seats_by_no]
            if not candidates:
                continue
            partner_tokens = [
                seat_token(seats_by_no[p.seat_no])
                for p in alive_wolves_all
                if p.seat_no != wolf.seat_no and p.seat_no in seats_by_no
            ]
            try:
                action = await self._ask(
                    game,
                    wolf,
                    seat,
                    all_players,
                    seats,
                    task_text=task_wolf_chat(partner_tokens, [seat_token(c) for c in candidates]),
                )
            except Exception:
                log.exception("llm wolf-chat ask failed for game %s seat %s", game.id, wolf.seat_no)
                continue
            if action.intent != "speak":
                continue
            message = action.public_message.strip()
            if not message:
                continue
            # Re-verify after slow LLM call — deadline / force-skip / abort /
            # victory can advance the game while _ask() is in flight. Mirrors
            # the guard in _maybe_speak(). Stale is a global condition (phase
            # or day has moved on for *all* wolves), so exit the loop entirely.
            fresh = await self.repo.load_game(game.id)
            if (
                fresh is None
                or fresh.ended_at is not None
                or fresh.phase is not Phase.NIGHT
                or fresh.day_number != game.day_number
            ):
                return
            try:
                await self.message_poster.post_wolves_chat(
                    fresh, f"**{seat.display_name}**: {message}", kind="WOLF_CHAT"
                )
            except Exception:
                log.exception("post_wolves_chat for LLM wolf failed seat %s", wolf.seat_no)
                continue
            now_ts = self._clock()
            for audience in alive_wolves_all:
                try:
                    await self.repo.insert_log_private(
                        LogEntry(
                            game_id=fresh.id,
                            day=fresh.day_number,
                            phase=fresh.phase,
                            kind="WOLF_CHAT",
                            actor_seat=wolf.seat_no,
                            audience_seat=audience.seat_no,
                            visibility="PRIVATE",
                            text=message,
                            created_at=now_ts,
                        )
                    )
                except Exception:
                    log.exception(
                        "WOLF_CHAT log insert failed game=%s actor=%s audience=%s",
                        fresh.id,
                        wolf.seat_no,
                        audience.seat_no,
                    )

    # ------------------------------------------------------ votes
    async def submit_llm_votes(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        candidates: Sequence[int] | None,
        round_: int,
        restrict_to_seats: frozenset[int] | None = None,
    ) -> None:
        """Schedule each LLM's vote in a background task. Fire-and-forget.

        `restrict_to_seats` lets `resend_pending_dms` re-dispatch for only the
        LLM seats that still owe a vote after `/wolf extend`.
        """
        seats_by_no = {s.seat_no: s for s in seats}
        llm_voters = [
            p
            for p in players
            if p.alive and p.seat_no in seats_by_no and seats_by_no[p.seat_no].is_llm
        ]
        if restrict_to_seats is not None:
            llm_voters = [p for p in llm_voters if p.seat_no in restrict_to_seats]
        if not llm_voters:
            return
        task = asyncio.create_task(
            self._run_votes(game, llm_voters, players, seats, candidates, round_),
            name=f"llm-votes-{game.id}-d{game.day_number}-r{round_}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_votes(
        self,
        game: Game,
        llm_voters: Sequence[Player],
        all_players: Sequence[Player],
        seats: Sequence[Seat],
        candidates: Sequence[int] | None,
        round_: int,
    ) -> None:
        seats_by_no = {s.seat_no: s for s in seats}
        expected_phase = Phase.DAY_VOTE if round_ == 0 else Phase.DAY_RUNOFF

        async def _one_vote(voter: Player) -> None:
            # Per-seat stale check, idempotency, and submission. try/except
            # contains per-seat failures so peers scheduled alongside via
            # asyncio.gather are not cancelled.
            try:
                fresh = await self.repo.load_game(game.id)
                if (
                    fresh is None
                    or fresh.ended_at is not None
                    or fresh.phase is not expected_phase
                    or fresh.day_number != game.day_number
                ):
                    return
                seat = seats_by_no[voter.seat_no]
                if seat.persona_key is None:
                    return
                # Idempotency: skip if this seat already has a vote row for
                # this round. Also guards the resend path (restrict_to_seats)
                # from double-submitting if the original task lands first.
                existing_votes = await self.repo.load_votes(
                    game.id, day=game.day_number, round_=round_
                )
                if any(v.voter_seat == voter.seat_no for v in existing_votes):
                    return
                if candidates is None:
                    cand_seats = [
                        s
                        for s in seats
                        if s.seat_no != voter.seat_no
                        and any(p.seat_no == s.seat_no and p.alive for p in all_players)
                    ]
                else:
                    cand_seats = [
                        s
                        for s in seats
                        if s.seat_no in set(candidates) and s.seat_no != voter.seat_no
                    ]
                if not cand_seats:
                    await self.gs.submit_vote(
                        game.id,
                        voter.seat_no,
                        target_seat=None,
                        round_=round_,
                        day=game.day_number,
                    )
                    return
                wolf_partner_tokens: list[str] = []
                if voter.role is Role.WEREWOLF:
                    wolf_partner_tokens = [
                        seat_token(seats_by_no[p.seat_no])
                        for p in all_players
                        if p.alive
                        and p.role is Role.WEREWOLF
                        and p.seat_no != voter.seat_no
                        and p.seat_no in seats_by_no
                    ]
                action = await self._ask(
                    game,
                    voter,
                    seat,
                    all_players,
                    seats,
                    task_text=task_vote(
                        [seat_token(c) for c in cand_seats],
                        runoff=round_ == 1,
                        role=voter.role,
                        wolf_partner_tokens=wolf_partner_tokens,
                    ),
                )
                if action.intent == "skip":
                    target = None
                else:
                    target = self._resolve_target(action.target_name, cand_seats, allow_none=False)
                await self.gs.submit_vote(
                    game.id,
                    voter.seat_no,
                    target_seat=target,
                    round_=round_,
                    day=game.day_number,
                )
            except Exception:
                log.exception(
                    "llm vote failed for game %s seat %s round %s",
                    game.id,
                    voter.seat_no,
                    round_,
                )

        # Dispatch per-seat in parallel.
        await asyncio.gather(*(_one_vote(v) for v in llm_voters))

    # --------------------------------------------------- daytime speeches
    async def submit_llm_discussion_rounds(
        self, game: Game, players: Sequence[Player], seats: Sequence[Seat]
    ) -> None:
        """Schedule a background task that has each alive LLM speak twice.

        Fire-and-forget. Each LLM speaks once in round 1 and once in round 2,
        in seat-no order, with jitter between speeches. Per-seat progress is
        persisted in `llm_speech_counts.discussion_rounds_done` regardless of
        decider success / skip / exception, so a single failure can't freeze
        the phase. After both rounds finish for all seats, the engine is woken
        so `_plan_next` can advance to DAY_VOTE.
        """
        seats_by_no = {s.seat_no: s for s in seats}
        llm_players = [
            p
            for p in players
            if p.alive and p.seat_no in seats_by_no and seats_by_no[p.seat_no].is_llm
        ]
        # Seed the phase baseline even when there are no LLM seats (all-human
        # game). Human text events recorded by on_message need the sentinel to
        # exist so PublicDiscussionState rebuild works.
        if self.discussion_service is not None:
            try:
                alive_seat_nos = sorted(p.seat_no for p in players if p.alive)
                await self.discussion_service.begin_phase_if_absent(
                    game_id=game.id,
                    day=game.day_number,
                    phase=game.phase,
                    alive_seat_nos=alive_seat_nos,
                )
            except Exception:
                log.exception(
                    "phase_baseline insert failed for %s day=%d", game.id, game.day_number
                )
        if not llm_players:
            return
        task = asyncio.create_task(
            self._run_discussion_rounds(game, llm_players, seats),
            name=f"llm-discussion-{game.id}-d{game.day_number}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_discussion_rounds(
        self,
        game: Game,
        llm_players: Sequence[Player],
        seats: Sequence[Seat],
    ) -> None:
        seats_by_no = {s.seat_no: s for s in seats}
        ordered = sorted(llm_players, key=lambda p: p.seat_no)
        # Phase baseline is seeded by submit_llm_discussion_rounds before the
        # background task starts — no need to duplicate it here.
        for round_idx in (1, 2):
            for llm in ordered:
                # Re-read live game state before each per-seat attempt so a
                # mid-round phase advance (force-skip / abort / victory) safely
                # short-circuits remaining work.
                fresh = await self.repo.load_game(game.id)
                if (
                    fresh is None
                    or fresh.ended_at is not None
                    or fresh.phase is not Phase.DAY_DISCUSSION
                    or fresh.day_number != game.day_number
                ):
                    return
                seat = seats_by_no.get(llm.seat_no)
                if seat is None or not seat.is_llm:
                    # Defensive: not really an LLM seat. Skip without bumping
                    # progress (won't enter the gate anyway).
                    continue
                if seat.persona_key is None:
                    # Misconfigured LLM seat — bump progress so the phase is
                    # not frozen forever waiting on a seat that can't speak.
                    await self.repo.increment_llm_discussion_round(
                        game.id, game.day_number, llm.seat_no
                    )
                    continue
                progress = await self.repo.load_llm_speech_progress(
                    game.id, game.day_number, llm.seat_no
                )
                if progress[3] >= round_idx:
                    # This seat already has this round done (recovery
                    # re-dispatch overlap). Skip without bumping.
                    continue
                try:
                    await self._do_one_discussion_speech(
                        game=fresh,
                        player=llm,
                        seat=seat,
                        seats=seats,
                        discussion_round=round_idx,
                    )
                except Exception:
                    log.exception(
                        "discussion speech failed game=%s seat=%s round=%d",
                        game.id,
                        llm.seat_no,
                        round_idx,
                    )
                finally:
                    # Always advance progress: success / skip / exception alike.
                    # Otherwise a single broken seat would freeze DAY_DISCUSSION
                    # forever in `_plan_next`.
                    await self.repo.increment_llm_discussion_round(
                        game.id, game.day_number, llm.seat_no
                    )
                try:
                    await asyncio.sleep(self.rng.uniform(*DISCUSSION_INTRA_ROUND_DELAY))
                except asyncio.CancelledError:
                    return
            if round_idx == 1:
                try:
                    await asyncio.sleep(self.rng.uniform(*DISCUSSION_INTER_ROUND_DELAY))
                except asyncio.CancelledError:
                    return
        # Wake the engine so `_plan_next` re-checks and advances to DAY_VOTE.
        try:
            gs = self._gs_slot.get("gs")
            if gs is not None:
                gs.wake.wake(game.id)
        except Exception:
            log.exception("wake after discussion rounds failed for %s", game.id)

    async def _do_one_discussion_speech(
        self,
        *,
        game: Game,
        player: Player,
        seat: Seat,
        seats: Sequence[Seat],
        discussion_round: int | None = None,
    ) -> None:
        players = await self.repo.load_players(game.id)
        action = await self._ask(
            game,
            player,
            seat,
            players,
            seats,
            task_text=task_daytime_speech(
                game.day_number,
                discussion_round=discussion_round,
                role=player.role,
            ),
        )
        if action.intent != "speak":
            return
        message = action.public_message.strip()
        if not message:
            return
        fresh = await self.repo.load_game(game.id)
        if (
            fresh is None
            or fresh.phase is not Phase.DAY_DISCUSSION
            or fresh.day_number != game.day_number
        ):
            return
        if self.message_poster is None:
            return
        try:
            await self.message_poster.post_public(
                fresh, f"**{seat.display_name}**: {message}", kind="LLM_SPEAK"
            )
        except Exception:
            log.exception("post_public for LLM discussion speech failed seat=%s", player.seat_no)
            return
        posted_at = self._clock()
        try:
            await self.repo.insert_log_public(
                LogEntry(
                    game_id=fresh.id,
                    day=fresh.day_number,
                    phase=fresh.phase,
                    kind="PLAYER_SPEECH",
                    actor_seat=player.seat_no,
                    visibility="PUBLIC",
                    text=message,
                    created_at=posted_at,
                )
            )
        except Exception:
            log.exception("PLAYER_SPEECH log insert failed for seat %s", player.seat_no)
        await self._emit_npc_speech_event(
            fresh, player.seat_no, message, co_declaration=action.co_declaration
        )

    # --------------------------------------------------- runoff candidate speech
    async def submit_llm_runoff_candidate_speeches(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        tied_candidates: Sequence[int],
    ) -> None:
        """Schedule a background task that has each tied LLM candidate speak once.

        Only candidates in `tied_candidates` whose seat is an LLM are scheduled;
        non-tied LLMs and human candidates are silent here. Per-seat progress
        marks `runoff_speech_done=1` in `finally` so the engine can advance to
        DAY_RUNOFF even if a single decider call fails.
        """
        seats_by_no = {s.seat_no: s for s in seats}
        candidate_set = set(tied_candidates)
        candidate_llm_players = [
            p
            for p in players
            if p.alive
            and p.seat_no in candidate_set
            and seats_by_no.get(p.seat_no) is not None
            and seats_by_no[p.seat_no].is_llm
        ]
        if not candidate_llm_players:
            return
        task = asyncio.create_task(
            self._run_runoff_candidate_speeches(game, candidate_llm_players, seats),
            name=f"llm-runoff-speech-{game.id}-d{game.day_number}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_runoff_candidate_speeches(
        self,
        game: Game,
        llm_players: Sequence[Player],
        seats: Sequence[Seat],
    ) -> None:
        seats_by_no = {s.seat_no: s for s in seats}
        ordered = sorted(llm_players, key=lambda p: p.seat_no)
        # Phase baseline for runoff: alive seats at runoff entry.
        if self.discussion_service is not None:
            try:
                all_players = await self.repo.load_players(game.id)
                alive_seat_nos = sorted(p.seat_no for p in all_players if p.alive)
                await self.discussion_service.begin_phase_if_absent(
                    game_id=game.id,
                    day=game.day_number,
                    phase=game.phase,
                    alive_seat_nos=alive_seat_nos,
                )
            except Exception:
                log.exception(
                    "runoff phase_baseline insert failed %s day=%d", game.id, game.day_number
                )
        for llm in ordered:
            fresh = await self.repo.load_game(game.id)
            if (
                fresh is None
                or fresh.ended_at is not None
                or fresh.phase is not Phase.DAY_RUNOFF_SPEECH
                or fresh.day_number != game.day_number
            ):
                return
            seat = seats_by_no.get(llm.seat_no)
            if seat is None or not seat.is_llm:
                continue
            if seat.persona_key is None:
                await self.repo.mark_llm_runoff_speech_done(game.id, game.day_number, llm.seat_no)
                continue
            progress = await self.repo.load_llm_speech_progress(
                game.id, game.day_number, llm.seat_no
            )
            if progress[4]:
                continue  # already done — recovery overlap
            try:
                await self._do_one_runoff_speech(game=fresh, player=llm, seat=seat, seats=seats)
            except Exception:
                log.exception(
                    "runoff candidate speech failed game=%s seat=%s",
                    game.id,
                    llm.seat_no,
                )
            finally:
                await self.repo.mark_llm_runoff_speech_done(game.id, game.day_number, llm.seat_no)
        try:
            gs = self._gs_slot.get("gs")
            if gs is not None:
                gs.wake.wake(game.id)
        except Exception:
            log.exception("wake after runoff speeches failed for %s", game.id)

    async def _do_one_runoff_speech(
        self,
        *,
        game: Game,
        player: Player,
        seat: Seat,
        seats: Sequence[Seat],
    ) -> None:
        players = await self.repo.load_players(game.id)
        action = await self._ask(
            game,
            player,
            seat,
            players,
            seats,
            task_text=task_daytime_speech(game.day_number, role=player.role),
        )
        if action.intent != "speak":
            return
        message = action.public_message.strip()
        if not message:
            return
        fresh = await self.repo.load_game(game.id)
        if (
            fresh is None
            or fresh.phase is not Phase.DAY_RUNOFF_SPEECH
            or fresh.day_number != game.day_number
        ):
            return
        if self.message_poster is None:
            return
        try:
            await self.message_poster.post_public(
                fresh, f"**{seat.display_name}**: {message}", kind="LLM_SPEAK"
            )
        except Exception:
            log.exception("post_public for LLM runoff speech failed seat=%s", player.seat_no)
            return
        posted_at = self._clock()
        try:
            await self.repo.insert_log_public(
                LogEntry(
                    game_id=fresh.id,
                    day=fresh.day_number,
                    phase=fresh.phase,
                    kind="PLAYER_SPEECH",
                    actor_seat=player.seat_no,
                    visibility="PUBLIC",
                    text=message,
                    created_at=posted_at,
                )
            )
        except Exception:
            log.exception("PLAYER_SPEECH log insert failed for seat %s", player.seat_no)
        await self._emit_npc_speech_event(
            fresh, player.seat_no, message, co_declaration=action.co_declaration
        )

    async def _emit_npc_speech_event(
        self,
        game: Game,
        speaker_seat: int,
        text: str,
        *,
        co_declaration: str | None = None,
    ) -> None:
        """Persist a SpeechEvent(source=npc_generated) for an LLM utterance.

        Wrapped in try/except so a SpeechEvent persistence hiccup does not
        rollback the already-completed Discord post + PLAYER_SPEECH log.
        Skips DiscussionService's own MessagePoster + LogSink hooks (they
        would duplicate the LLM speech) by going through `_store.insert`
        directly when available.
        """
        if self.discussion_service is None:
            return
        try:
            from wolfbot.domain.discussion import make_phase_id
            from wolfbot.services.discussion_service import (
                make_npc_generated_event,
            )

            phase_id = make_phase_id(game.id, game.day_number, game.phase)
            event = make_npc_generated_event(
                game_id=game.id,
                phase_id=phase_id,
                day=game.day_number,
                phase=game.phase,
                speaker_seat=speaker_seat,
                text=text,
                co_declaration=co_declaration,
            )
            # Persist-only avoids re-posting the message to Discord (the
            # rounds-mode path already posted it via MessagePoster) and
            # avoids re-inserting the PLAYER_SPEECH LogEntry (already inserted).
            await self.discussion_service.record_persist_only(event)
        except Exception:
            log.exception(
                "SpeechEvent(npc_generated) write failed for game=%s seat=%s",
                game.id,
                speaker_seat,
            )

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
        persona = NPC_PERSONAS_BY_KEY.get(seat.persona_key or "")
        if persona is None:
            return LLMAction(intent="skip", reason_summary="persona missing")
        assert player.role is not None
        public_logs = await self.repo.load_public_logs(game.id, limit=40)
        private_logs = await self.repo.load_private_logs_for_audience(
            game.id, audience_seat=player.seat_no, limit=40
        )
        deduced_block = await self._build_deduced_facts_block(game, players, seats)
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
            deduced_facts_block=deduced_block,
        )
        actor = (
            f"seat={player.seat_no} persona={seat.persona_key} role={player.role.value}"
        )
        task_tag = _classify_task_text(task_text)
        with trace_context(
            game_id=game.id,
            phase=game.phase.value,
            day=game.day_number,
            actor=actor,
            metadata={"task": task_tag},
        ):
            try:
                return await self.decider.decide(system, user)
            except Exception:
                log.exception(
                    "LLM decide failed for seat %s game %s", player.seat_no, game.id
                )
                return LLMAction(intent="skip", reason_summary="decider error")

    async def _build_deduced_facts_block(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
    ) -> str | None:
        """Assemble Master's HARD/MEDIUM deduced facts for this turn.

        Pulls CO history from the SpeechEvent fold and per-day vote rows
        from the repo, then runs them through `deduction_service.deduce`.
        Returns ``None`` (rather than the empty-marker string) on any
        failure so the user-context template skips the section instead of
        showing a confusing '(該当なし)' line.
        """
        try:
            from wolfbot.services.deduction_service import deduce, render_facts_block
            from wolfbot.services.discussion_service import rebuild_public_state_from_events

            co_claims: tuple[CoClaim, ...] = ()
            if self.discussion_service is not None:
                phase_id = make_phase_id(game.id, game.day_number, game.phase)
                events = await self.discussion_service.load_phase(game.id, phase_id)
                state = rebuild_public_state_from_events(events)
                if state is not None:
                    co_claims = state.co_claims

            votes_by_day: dict[int, list[Vote]] = {}
            for past_day in range(1, game.day_number):
                votes = await self.repo.load_votes(game.id, past_day, 0)
                if votes:
                    votes_by_day[past_day] = votes

            facts = deduce(
                co_claims=co_claims,
                players=players,
                seats=seats,
                votes_by_day=votes_by_day,
            )
            if not facts:
                return None
            return render_facts_block(facts)
        except Exception:
            log.exception("deduce facts failed for game %s seat", game.id)
            return None

    def _resolve_target(
        self,
        target_name: str | None,
        candidates: Sequence[Seat],
        *,
        allow_none: bool,
    ) -> int | None:
        """Map an LLM `target_name` back to a seat_no.

        Candidates are presented to the LLM as `席{N} {display_name}` tokens. The
        primary path parses the leading `席N` prefix, which disambiguates duplicate
        display_names. Bare-name matches are a legacy fallback and only accepted
        when exactly one candidate has that display_name.
        """
        if target_name is None:
            if allow_none:
                return None
            pick = self.rng.choice(list(candidates))
            log.warning("LLM returned null target; fallback to seat %d", pick.seat_no)
            return pick.seat_no
        m = _SEAT_TOKEN_RE.match(target_name)
        if m is not None:
            seat_no = int(m.group(1))
            for c in candidates:
                if c.seat_no == seat_no:
                    return c.seat_no
        matches = [c for c in candidates if c.display_name == target_name]
        if len(matches) == 1:
            return matches[0].seat_no
        log.warning(
            "LLM target_name %r not resolvable (ambiguous or unknown); random fallback",
            target_name,
        )
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
_XAI_DEFAULT_BASE_URL = "https://api.x.ai/v1"
_DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"


def make_xai_decider(
    api_key: str,
    model: str,
    *,
    base_url: str | None = None,
    timeout: float = 30.0,
) -> XAILLMActionDecider:
    """Build an OpenAI-Chat-Completions-compatible decider (xAI default).

    ``base_url`` defaults to xAI Grok; override to point at OpenAI, Groq,
    Together, vLLM, Ollama, or any other strict-json_schema-supporting
    OpenAI-compat endpoint.  Imports ``openai`` lazily so tests skipping
    LLM paths don't need the dep installed.
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=base_url or _XAI_DEFAULT_BASE_URL)
    return XAILLMActionDecider(client=client, model=model, timeout=timeout)


def make_deepseek_decider(
    api_key: str,
    model: str,
    *,
    base_url: str | None = None,
    thinking: Literal["enabled", "disabled"] = "enabled",
    reasoning_effort: Literal["high", "max"] = "max",
    timeout: float = 30.0,
) -> DeepSeekLLMActionDecider:
    """Build a DeepSeek-backed decider. Imports openai lazily."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=base_url or _DEEPSEEK_DEFAULT_BASE_URL)
    return DeepSeekLLMActionDecider(
        client=client,
        model=model,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )


def make_gemini_decider(
    project: str,
    location: str,
    model: str,
    *,
    thinking_level: Literal["minimal", "low", "medium", "high"] = "high",
    timeout: float = 30.0,
) -> GeminiLLMActionDecider:
    """Build a Vertex AI Gemini-backed decider. Imports google-genai lazily.

    Authenticates via Application Default Credentials (gcloud locally,
    attached service account in production). `timeout` is forwarded as
    `HttpOptions(timeout=...)` (milliseconds) — the SDK does not accept
    a per-call `timeout=` like the openai client does.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=types.HttpOptions(timeout=int(timeout * 1000)),
    )
    return GeminiLLMActionDecider(
        client=client,
        model=model,
        thinking_level=thinking_level,
        timeout=timeout,
    )


def make_llm_decider(cfg: LLMDeciderConfig) -> LLMActionDecider:
    """Provider-aware decider factory. Branches on ``cfg.provider``.

    The Settings model_validator that built ``cfg`` guarantees the
    relevant credential is non-None / non-empty by the time we get here;
    the asserts are a documentation aid for mypy.

    The same factory is used for both Master's Gameplay LLM (built from
    ``MasterSettings.gameplay_decider_config()``) and any ad-hoc gameplay
    decider tests need.  NPC speech generation has its own factory in
    :mod:`wolfbot.npc.generator_factory` because the NPC schema differs.
    """
    if cfg.provider == "xai":
        assert cfg.api_key is not None  # validated in Settings
        return make_xai_decider(
            api_key=cfg.api_key.get_secret_value(),
            model=cfg.model,
            base_url=cfg.base_url,
            timeout=cfg.timeout,
        )
    if cfg.provider == "deepseek":
        assert cfg.api_key is not None  # validated in Settings
        return make_deepseek_decider(
            api_key=cfg.api_key.get_secret_value(),
            model=cfg.model,
            base_url=cfg.base_url,
            thinking=cfg.thinking,
            reasoning_effort=cfg.reasoning_effort,
            timeout=cfg.timeout,
        )
    if cfg.provider == "gemini":
        assert cfg.vertex_project is not None  # validated in Settings
        return make_gemini_decider(
            project=cfg.vertex_project,
            location=cfg.vertex_location,
            model=cfg.model,
            thinking_level=cfg.thinking_level,
            timeout=cfg.timeout,
        )
    if cfg.provider == "mock":
        return MockLLMActionDecider()
    raise ValueError(f"unknown provider: {cfg.provider!r}")


__all__ = [
    "RESPONSE_SCHEMA",
    "DeepSeekLLMActionDecider",
    "FakeLLMActionDecider",
    "GeminiLLMActionDecider",
    "LLMAction",
    "LLMActionDecider",
    "LLMAdapter",
    "MockLLMActionDecider",
    "XAILLMActionDecider",
    "make_deepseek_decider",
    "make_gemini_decider",
    "make_llm_decider",
    "make_xai_decider",
]

# keep Phase referenced for mypy
_ = Phase
