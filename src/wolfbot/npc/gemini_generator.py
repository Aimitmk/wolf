"""Vertex AI Gemini-backed NPC speech generator.

Mirror of :mod:`wolfbot.npc.openai_compatible_generator` but talks to
Vertex AI's Gemini API via the official ``google-genai`` SDK with
``response_mime_type="application/json"`` + ``response_json_schema`` for
structured output and ``ThinkingConfig.thinking_level`` for thinking
control.

Authentication is ADC/IAM only — same model as
:class:`wolfbot.services.llm_service.GeminiLLMActionDecider`.  Vertex
AI Express mode and API-key auth are deliberately unsupported (use the
OpenAI-compat generator with a different provider if you need API-key
auth).

Gemini's internal thinking / thought signatures are deliberately never
read or persisted — only ``resp.text`` is consumed, mirroring the
DeepSeek / xAI paths.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

from wolfbot.domain.ws_messages import LogicPacket, SpeakRequest
from wolfbot.npc.openai_compatible_generator import (
    _RESPONSE_SCHEMA,
    _build_speech_from_json,
    _build_system,
    _build_user,
)
from wolfbot.npc.personas import NPC_PERSONAS_BY_KEY
from wolfbot.npc.speech_service import NpcGeneratedSpeech

log = logging.getLogger(__name__)


@dataclass
class GeminiVertexConfig:
    """Gemini config for the NPC speech generator.

    Supports two authentication modes (mutually exclusive at the
    constructor boundary, picked by the factory based on which env
    var was set):

      * Vertex AI / ADC — set ``project`` (and optionally
        ``location``); leave ``api_key=None``.
      * Google AI Studio — set ``api_key`` (``AIza...`` form);
        leave ``project=None``.

    The class name is unchanged for back-compat with imports / tests
    even though the AI Studio mode is no longer strictly "vertex".
    """

    project: str | None = None
    location: str = "global"
    model: str = "gemini-3-flash-preview"
    thinking_level: Literal["minimal", "low", "medium", "high"] = "high"
    timeout: float = 15.0
    api_key: str | None = None


class GeminiNpcGenerator:
    """NpcGenerator backed by Vertex AI Gemini.

    The system / user prompt and persona machinery are reused verbatim
    from the OpenAI-compat generator.  Only the request shape changes:
    Vertex Gemini wants ``GenerateContentConfig.system_instruction`` +
    ``response_json_schema`` instead of ``messages[]`` +
    ``response_format``.
    """

    def __init__(self, *, config: GeminiVertexConfig) -> None:
        self.config = config
        self._persona_key: str | None = None

    def set_persona(self, persona_key: str) -> None:
        if persona_key not in NPC_PERSONAS_BY_KEY:
            valid = ", ".join(sorted(NPC_PERSONAS_BY_KEY.keys()))
            raise ValueError(f"unknown persona_key {persona_key!r}; valid keys: {valid}")
        self._persona_key = persona_key

    async def generate(
        self,
        *,
        logic: LogicPacket,
        request: SpeakRequest,
        state: object | None = None,
    ) -> NpcGeneratedSpeech | None:
        from google import genai
        from google.genai import types

        from wolfbot.services.llm_trace import (
            CallTimer,
            extract_gemini_vertex_tokens,
            log_llm_call,
            parse_day_from_phase_id,
            parse_game_id_from_phase_id,
            trace_context,
        )

        if self._persona_key is None:
            raise RuntimeError(
                "GeminiNpcGenerator.generate() called before set_persona(); "
                "each NPC bot must declare its persona at startup."
            )
        persona = NPC_PERSONAS_BY_KEY[self._persona_key]
        # Phase-D: prefer state.role; fall back to SpeakRequest.role.
        role_value = getattr(state, "role", None) or request.role
        system = _build_system(
            persona,
            max_chars=request.max_chars,
            role=role_value,
            role_strategy=request.role_strategy,
        )
        user = _build_user(logic, request, state)

        # Two SDK construction modes — Vertex (ADC) or AI Studio (api
        # key). At least one must be set per ``NpcSettings`` validator;
        # both set means the factory passed both, so prefer Vertex.
        if self.config.project:
            client = genai.Client(
                vertexai=True,
                project=self.config.project,
                location=self.config.location,
                http_options=types.HttpOptions(
                    timeout=int(self.config.timeout * 1000),
                ),
            )
        elif self.config.api_key:
            client = genai.Client(
                api_key=self.config.api_key,
                http_options=types.HttpOptions(
                    timeout=int(self.config.timeout * 1000),
                ),
            )
        else:
            raise RuntimeError(
                "GeminiNpcGenerator requires either project (Vertex AI / ADC) "
                "or api_key (Google AI Studio); both are unset."
            )

        actor = f"npc_id={request.npc_id} seat={request.seat_no} persona={self._persona_key}"
        timer = CallTimer()
        content = ""
        err: str | None = None
        tokens: dict[str, int | None] | None = None
        with trace_context(
            game_id=parse_game_id_from_phase_id(request.phase_id),
            phase=request.phase_id,
            day=parse_day_from_phase_id(request.phase_id),
            actor=actor,
            metadata={
                "request_id": request.request_id,
                "logic_packet_id": request.logic_packet_id,
                "suggested_intent": request.suggested_intent,
                "max_chars": request.max_chars,
                "thinking_level": self.config.thinking_level,
            },
        ):
            try:
                from wolfbot.services.llm_service import (
                    _gemini_thinking_config,
                )

                thinking_cfg = _gemini_thinking_config(
                    self.config.model,
                    self.config.thinking_level,
                )
                gc_kwargs: dict[str, object] = {
                    "system_instruction": system,
                    "response_mime_type": "application/json",
                    "response_json_schema": _RESPONSE_SCHEMA["schema"],
                }
                if thinking_cfg is not None:
                    gc_kwargs["thinking_config"] = thinking_cfg
                resp = await client.aio.models.generate_content(
                    model=self.config.model,
                    contents=user,
                    config=types.GenerateContentConfig(**gc_kwargs),  # type: ignore[arg-type]
                )
                content = resp.text or "{}"
                tokens = extract_gemini_vertex_tokens(resp)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                log.exception(
                    "npc_generate_gemini_failed project=%s model=%s",
                    self.config.project,
                    self.config.model,
                )
                await log_llm_call(
                    role="npc_speech",
                    provider="gemini",
                    model=self.config.model,
                    system_prompt=system,
                    user_prompt=user,
                    response=None,
                    latency_ms=timer.elapsed_ms,
                    error=err,
                    file_stem=f"npc_{self._persona_key}",
                )
                return None

            await log_llm_call(
                role="npc_speech",
                provider="gemini",
                model=self.config.model,
                system_prompt=system,
                user_prompt=user,
                response=content,
                latency_ms=timer.elapsed_ms,
                error=None,
                tokens=tokens,
                file_stem=f"npc_{self._persona_key}",
            )

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            log.warning("npc_generate_invalid_json response=%s", content[:200])
            return None

        return _build_speech_from_json(data)

    async def decide_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, object],
    ) -> str:
        """Phase-D: structured-output decision call (vote / night action).

        Mirrors :meth:`OpenAICompatibleNpcGenerator.decide_json` so the NPC
        bot's ``DecisionLLM`` protocol detection picks up Gemini-backed
        generators too. Without this method, the npc/main.py wiring code
        (line ~226) silently falls back to ``decision_llm=None`` and every
        vote / night-action returns "abstain", which is what bricked
        game ``75a3b1f379cc`` after the gemini provider switch.

        Uses the same Vertex / AI Studio client construction as the
        speech path (project XOR api_key on the config) and the same
        thinking-config helper that adapts ``thinking_level`` /
        ``thinking_budget`` per model family.
        """
        from google import genai
        from google.genai import types

        from wolfbot.services.llm_service import _gemini_thinking_config
        from wolfbot.services.llm_trace import (
            CallTimer,
            extract_gemini_vertex_tokens,
            log_llm_call,
        )

        if self.config.project:
            client = genai.Client(
                vertexai=True,
                project=self.config.project,
                location=self.config.location,
                http_options=types.HttpOptions(
                    timeout=int(self.config.timeout * 1000),
                ),
            )
        elif self.config.api_key:
            client = genai.Client(
                api_key=self.config.api_key,
                http_options=types.HttpOptions(
                    timeout=int(self.config.timeout * 1000),
                ),
            )
        else:
            raise RuntimeError(
                "GeminiNpcGenerator.decide_json requires either project "
                "(Vertex AI / ADC) or api_key (Google AI Studio)."
            )

        thinking_cfg = _gemini_thinking_config(
            self.config.model,
            self.config.thinking_level,
        )
        gc_kwargs: dict[str, object] = {
            "system_instruction": system_prompt,
            "response_mime_type": "application/json",
            "response_json_schema": schema,
        }
        if thinking_cfg is not None:
            gc_kwargs["thinking_config"] = thinking_cfg

        timer = CallTimer()
        content = ""
        err: str | None = None
        tokens: dict[str, int | None] | None = None
        try:
            resp = await client.aio.models.generate_content(
                model=self.config.model,
                contents=user_prompt,
                config=types.GenerateContentConfig(**gc_kwargs),  # type: ignore[arg-type]
            )
            content = resp.text or "{}"
            tokens = extract_gemini_vertex_tokens(resp)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            log.exception(
                "npc_decide_gemini_failed project=%s model=%s",
                self.config.project,
                self.config.model,
            )
            await log_llm_call(
                role="npc_decision",
                provider="gemini",
                model=self.config.model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response=None,
                latency_ms=timer.elapsed_ms,
                error=err,
                file_stem=f"npc_{self._persona_key}",
            )
            raise

        await log_llm_call(
            role="npc_decision",
            provider="gemini",
            model=self.config.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response=content,
            latency_ms=timer.elapsed_ms,
            error=None,
            tokens=tokens,
            file_stem=f"npc_{self._persona_key}",
        )
        return content


__all__ = ["GeminiNpcGenerator", "GeminiVertexConfig"]
