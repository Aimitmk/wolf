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
    """Vertex AI Gemini config.  Authentication is via ADC/IAM."""

    project: str
    location: str = "global"
    model: str = "gemini-3-flash-preview"
    thinking_level: Literal["minimal", "low", "medium", "high"] = "high"
    timeout: float = 15.0


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
            raise ValueError(
                f"unknown persona_key {persona_key!r}; valid keys: {valid}"
            )
        self._persona_key = persona_key

    async def generate(
        self,
        *,
        logic: LogicPacket,
        request: SpeakRequest,
    ) -> NpcGeneratedSpeech | None:
        from google import genai
        from google.genai import types

        if self._persona_key is None:
            raise RuntimeError(
                "GeminiNpcGenerator.generate() called before set_persona(); "
                "each NPC bot must declare its persona at startup."
            )
        persona = NPC_PERSONAS_BY_KEY[self._persona_key]
        system = _build_system(persona, max_chars=request.max_chars)
        user = _build_user(logic, request)

        client = genai.Client(
            vertexai=True,
            project=self.config.project,
            location=self.config.location,
            http_options=types.HttpOptions(timeout=int(self.config.timeout * 1000)),
        )
        try:
            resp = await client.aio.models.generate_content(
                model=self.config.model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_json_schema=_RESPONSE_SCHEMA["schema"],
                    thinking_config=types.ThinkingConfig(
                        # The SDK normalizes the string into ThinkingLevel
                        # at runtime; the type annotation is enum-only.
                        thinking_level=self.config.thinking_level,  # type: ignore[arg-type]
                    ),
                ),
            )
        except Exception:
            log.exception(
                "npc_generate_gemini_failed project=%s model=%s",
                self.config.project, self.config.model,
            )
            return None

        content = resp.text or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            log.warning("npc_generate_invalid_json response=%s", content[:200])
            return None

        return _build_speech_from_json(data)


__all__ = ["GeminiNpcGenerator", "GeminiVertexConfig"]
