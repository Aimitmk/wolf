"""NPC-side: take a `LogicPacket` + `SpeakRequest` and return a `SpeakResult`.

This is the NPC bot's Grok prompt-builder + structured-output call wrapped
in a deterministic Protocol so tests can substitute a `FakeNpcGenerator`.

The persona registry, role-strategy isolation, structured-output `LLMAction`
schema, and seat-token resolver come from the existing `wolfbot.llm` package
— this module only adds the LogicPacket-aware prompt assembly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from wolfbot.domain.ws_messages import LogicPacket, SpeakRequest, SpeakResult

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NpcGeneratedSpeech:
    text: str
    intent: str
    used_logic_ids: tuple[str, ...]
    estimated_duration_ms: int


@runtime_checkable
class NpcGenerator(Protocol):
    async def generate(
        self,
        *,
        logic: LogicPacket,
        request: SpeakRequest,
    ) -> NpcGeneratedSpeech | None: ...


class FakeNpcGenerator:
    """Returns a scripted utterance, or None to simulate decline."""

    def __init__(
        self,
        scripted: list[NpcGeneratedSpeech | None] | None = None,
        default: NpcGeneratedSpeech | None = None,
    ) -> None:
        self._scripted = list(scripted or [])
        self._default = default
        self.call_count = 0
        self.received_logic: list[LogicPacket] = []
        self.received_requests: list[SpeakRequest] = []

    async def generate(
        self,
        *,
        logic: LogicPacket,
        request: SpeakRequest,
    ) -> NpcGeneratedSpeech | None:
        self.call_count += 1
        self.received_logic.append(logic)
        self.received_requests.append(request)
        if self._scripted:
            return self._scripted.pop(0)
        return self._default


class NpcSpeechService:
    """Compose `SpeakResult` from a generator's output.

    Encapsulates length-cap enforcement and decline-on-empty so the NPC
    bot's main loop stays tidy.
    """

    def __init__(self, generator: NpcGenerator) -> None:
        self.generator = generator

    async def respond(
        self,
        *,
        logic: LogicPacket,
        request: SpeakRequest,
        now_ms: int,
    ) -> SpeakResult:
        try:
            speech = await self.generator.generate(logic=logic, request=request)
        except Exception:
            log.exception(
                "npc_generate_failed npc_id=%s req=%s", request.npc_id, request.request_id
            )
            return SpeakResult(
                ts=now_ms,
                trace_id=request.trace_id,
                request_id=request.request_id,
                npc_id=request.npc_id,
                phase_id=request.phase_id,
                status="error",
                failure_reason="generator_error",
            )
        if speech is None or not speech.text.strip():
            return SpeakResult(
                ts=now_ms,
                trace_id=request.trace_id,
                request_id=request.request_id,
                npc_id=request.npc_id,
                phase_id=request.phase_id,
                status="declined",
                failure_reason="speaker_declined",
            )
        text = speech.text.strip()
        if len(text) > request.max_chars:
            text = text[: request.max_chars]
        return SpeakResult(
            ts=now_ms,
            trace_id=request.trace_id,
            request_id=request.request_id,
            npc_id=request.npc_id,
            phase_id=request.phase_id,
            status="accepted",
            text=text,
            used_logic_ids=speech.used_logic_ids,
            intent=speech.intent,
            estimated_duration_ms=speech.estimated_duration_ms,
        )


__all__ = [
    "FakeNpcGenerator",
    "NpcGeneratedSpeech",
    "NpcGenerator",
    "NpcSpeechService",
]
