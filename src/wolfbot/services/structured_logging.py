"""Shared structured logging helper for master / voice-ingest / NPC workers.

Per the day-discussion + voice-ingest + npc-voice-pipeline specs, every
in-band log entry must carry the cross-cutting fields `ts`, `level`,
`component`, `event`, `game_id`, `phase_id`, `trace_id`, `span_id` (when
known) plus event-specific fields. We standardize on Python's stdlib
`logging` with a small adapter that injects required fields and produces
JSON-shaped messages.

This module is dependency-light so all three components can import it.
The `discussion_phase_summary` event is emitted from the Master at every
public-speech phase end; helper builders return the fully-shaped dict so
tests can assert presence and specific counts.

Test surface: ``CapturingHandler`` records every emitted event so tests
can assert ``events_with(name=..., game_id=...)`` without mocking stdlib
logging primitives.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

# Standardized component identifiers used in `component=` log fields.
COMPONENT_MASTER = "master"
COMPONENT_VOICE_INGEST = "voice-ingest"
COMPONENT_NPC_BOT = "npc-bot"


def _now_ms() -> int:
    return int(time.time() * 1000)


def emit_event(
    logger: logging.Logger,
    *,
    event: str,
    component: str,
    level: int = logging.INFO,
    game_id: str | None = None,
    phase_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    **fields: Any,
) -> None:
    """Emit a structured log event with the canonical envelope fields.

    Underlying logging keeps the event in `extra` so `CapturingHandler`
    can introspect it without parsing a JSON-formatted message.
    """
    payload: dict[str, Any] = {
        "ts": _now_ms(),
        "component": component,
        "event": event,
        "game_id": game_id,
        "phase_id": phase_id,
        "trace_id": trace_id,
        "span_id": span_id,
        **fields,
    }
    logger.log(level, "%s", event, extra={"structured_event": payload})


@dataclass
class _CapturedEvent:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)


class CapturingHandler(logging.Handler):
    """Test-only handler that records every structured event.

    Use via:

        handler = CapturingHandler()
        logging.getLogger("wolfbot").addHandler(handler)
        ...
        handler.events_with(name="discussion_phase_summary", game_id="g1")
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.events: list[_CapturedEvent] = []

    def emit(self, record: logging.LogRecord) -> None:
        payload = getattr(record, "structured_event", None)
        if not isinstance(payload, dict):
            return
        name = payload.get("event")
        if name is None:
            return
        self.events.append(_CapturedEvent(name=str(name), payload=dict(payload)))

    def events_with(self, *, name: str, **filters: Any) -> Iterator[_CapturedEvent]:
        for ev in self.events:
            if ev.name != name:
                continue
            if all(ev.payload.get(k) == v for k, v in filters.items()):
                yield ev


# ---------------------------------------------------------------- builders


def build_discussion_phase_summary(
    *,
    game_id: str,
    phase_id: str,
    mode: str,
    speech_events_total: int,
    human_speech_events: int,
    npc_speech_events: int,
    stt_success: int = 0,
    stt_failed: int = 0,
    logic_packets_built: int = 0,
    speak_requests_sent: int = 0,
    speak_results_accepted: int = 0,
    speak_results_rejected: int = 0,
    playback_authorized: int = 0,
    tts_success: int = 0,
    tts_failed: int = 0,
    playback_success: int = 0,
    playback_failed: int = 0,
    stale_dropped: int = 0,
) -> dict[str, Any]:
    """Build the `discussion_phase_summary` payload (without emitting).

    Master calls ``emit_event(... event="discussion_phase_summary", **payload)``
    once per phase end with this shape.
    """
    return {
        "game_id": game_id,
        "phase_id": phase_id,
        "mode": mode,
        "speech_events_total": speech_events_total,
        "human_speech_events": human_speech_events,
        "npc_speech_events": npc_speech_events,
        "stt_success": stt_success,
        "stt_failed": stt_failed,
        "logic_packets_built": logic_packets_built,
        "speak_requests_sent": speak_requests_sent,
        "speak_results_accepted": speak_results_accepted,
        "speak_results_rejected": speak_results_rejected,
        "playback_authorized": playback_authorized,
        "tts_success": tts_success,
        "tts_failed": tts_failed,
        "playback_success": playback_success,
        "playback_failed": playback_failed,
        "stale_dropped": stale_dropped,
    }


__all__ = [
    "COMPONENT_MASTER",
    "COMPONENT_NPC_BOT",
    "COMPONENT_VOICE_INGEST",
    "CapturingHandler",
    "build_discussion_phase_summary",
    "emit_event",
]
