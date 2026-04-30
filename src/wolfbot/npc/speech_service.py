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

from wolfbot.domain.enums import CO_CLAIM_VALUES, CoDeclaration
from wolfbot.domain.ws_messages import (
    ClaimedMediumResult,
    ClaimedSeerResult,
    LogicPacket,
    SpeakRequest,
    SpeakResult,
)
from wolfbot.npc.game_state import NpcGameState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NpcGeneratedSpeech:
    text: str
    intent: str
    used_logic_ids: tuple[str, ...]
    estimated_duration_ms: int
    co_declaration: str | None = None
    addressed_seat_no: int | None = None
    addressed_seat_nos: tuple[int, ...] = ()
    # Structured divination/medium claim attached to this utterance.
    # Populated when the LLM declares a NEW seer/medium result (real or
    # fake) so Master can build a per-seat claim history that anchors
    # future fake seers to their prior lies. None means the speech does
    # not announce a new result (general talk, references to prior
    # claims, non-seer speech).
    claimed_seer_target_seat: int | None = None
    claimed_seer_is_wolf: bool | None = None
    claimed_medium_target_seat: int | None = None
    claimed_medium_is_wolf: bool | None = None


@runtime_checkable
class NpcGenerator(Protocol):
    async def generate(
        self,
        *,
        logic: LogicPacket,
        request: SpeakRequest,
        state: NpcGameState | None = None,
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
        self.received_state: list[NpcGameState | None] = []

    async def generate(
        self,
        *,
        logic: LogicPacket,
        request: SpeakRequest,
        state: NpcGameState | None = None,
    ) -> NpcGeneratedSpeech | None:
        self.call_count += 1
        self.received_logic.append(logic)
        self.received_requests.append(request)
        self.received_state.append(state)
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
        state: NpcGameState | None = None,
    ) -> SpeakResult:
        try:
            speech = await self.generator.generate(
                logic=logic, request=request, state=state,
            )
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
        co_declaration: CoDeclaration | None = None
        if speech.co_declaration in CO_CLAIM_VALUES:
            co_declaration = speech.co_declaration  # type: ignore[assignment]
        # Drop self-address (==speaker_seat) defensively. We can't validate
        # alive / on-roster here without seat data; Master applies the
        # alive/self-filter when persisting, so a hallucinated seat number
        # is filtered out at the boundary, not silently dropped here.
        # Build the canonical addressed list, falling back to the legacy
        # singular field for back-compat with NpcGeneratedSpeech instances
        # that haven't been updated to populate the list.
        merged: list[int] = []
        for s in speech.addressed_seat_nos:
            if s is not None and s != request.seat_no and s not in merged:
                merged.append(int(s))
        if (
            speech.addressed_seat_no is not None
            and speech.addressed_seat_no != request.seat_no
            and speech.addressed_seat_no not in merged
        ):
            merged.append(int(speech.addressed_seat_no))
        addressed_seat_nos: tuple[int, ...] = tuple(merged)
        addressed_seat_no = addressed_seat_nos[0] if addressed_seat_nos else None

        claimed_seer = _build_claimed_seer(speech, speaker_seat=request.seat_no)
        claimed_medium = _build_claimed_medium(speech, speaker_seat=request.seat_no)

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
            co_declaration=co_declaration,
            claimed_seer_result=claimed_seer,
            claimed_medium_result=claimed_medium,
            addressed_seat_no=addressed_seat_no,
            addressed_seat_nos=addressed_seat_nos,
        )


def _build_claimed_seer(
    speech: NpcGeneratedSpeech, *, speaker_seat: int
) -> ClaimedSeerResult | None:
    """Validate the speech's seer claim and project it onto the wire model.

    Drops the claim defensively when:
      * target_seat is missing or out of range,
      * is_wolf is missing (a verdict is required for a seer claim),
      * the claim targets the speaker themselves (a real seer never
        divines their own seat in this ruleset).

    The drop is silent — we still send the speech, just without the
    structured claim. Master logs the omission via the absence of a
    matching record in the claim-history fold.
    """
    seat = speech.claimed_seer_target_seat
    verdict = speech.claimed_seer_is_wolf
    if seat is None or verdict is None:
        return None
    if not 1 <= seat <= 9:
        return None
    if seat == speaker_seat:
        return None
    return ClaimedSeerResult(target_seat=seat, is_wolf=verdict)


def _build_claimed_medium(
    speech: NpcGeneratedSpeech, *, speaker_seat: int
) -> ClaimedMediumResult | None:
    """Validate the speech's medium claim and project it onto the wire model.

    Mirrors ``_build_claimed_seer`` but allows ``is_wolf=None`` to encode
    "no execution yesterday → no result today". Self-target is still
    rejected — the only way a medium claims their own seat is via a
    coordinated lynching scenario that doesn't apply mid-discussion.
    """
    seat = speech.claimed_medium_target_seat
    verdict = speech.claimed_medium_is_wolf
    if seat is None:
        return None
    if not 1 <= seat <= 9:
        return None
    if seat == speaker_seat:
        return None
    return ClaimedMediumResult(target_seat=seat, is_wolf=verdict)


__all__ = [
    "FakeNpcGenerator",
    "NpcGeneratedSpeech",
    "NpcGenerator",
    "NpcSpeechService",
]
