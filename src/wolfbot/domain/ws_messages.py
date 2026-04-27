"""Master ↔ NPC and Master ↔ voice-ingest WebSocket message schemas.

All messages share a common envelope with `type`, `ts`, and `trace_id` fields.
Pydantic v2 frozen models give us strict structural validation at the wire
boundary. Discriminator on `type` lets the server dispatch by message kind
without touching dict-shaped JSON.

Design choices:
- One file holds every typed message so the protocol is reviewable in one diff.
- `BaseEnvelope` carries the cross-cutting fields; concrete messages inherit it.
- All payloads serialize/deserialize via `.model_dump_json()` /
  `.model_validate_json()` — no hand-rolled dict shaping.
- The runtime PSK handshake is enforced by the WS server before message
  parsing begins (so an unauthenticated peer cannot inject typed messages).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BaseEnvelope(BaseModel):
    """Cross-cutting fields present on every message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: int = Field(description="Wall-clock millis at send time")
    trace_id: str = Field(description="Per-flow correlation id")


# ---------------------------------------------------------------- NPC ↔ Master


class NpcRegister(BaseEnvelope):
    type: Literal["npc_register"] = "npc_register"
    npc_id: str
    discord_bot_user_id: str
    persona_key: str = Field(
        description=(
            "Persona this NPC bot embodies (key from wolfbot.npc.personas). "
            "Each NPC bot process is bound to exactly one persona at startup; "
            "Master uses this when filling reactive_voice LLM seats."
        )
    )
    supported_voices: tuple[str, ...] = ()
    version: str = "0.0.0"


class NpcRegistered(BaseEnvelope):
    type: Literal["npc_registered"] = "npc_registered"
    npc_id: str
    assigned_seat: int | None = None
    game_id: str | None = None
    phase_id: str | None = None


class Heartbeat(BaseEnvelope):
    type: Literal["heartbeat"] = "heartbeat"
    npc_id: str | None = None  # populated by NPC bots; None for voice-ingest


class LogicCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    claim: str
    support: tuple[str, ...] = ()
    counter: tuple[str, ...] = ()


class LogicPacket(BaseEnvelope):
    type: Literal["logic_packet"] = "logic_packet"
    packet_id: str
    phase_id: str
    recipient_npc_id: str
    public_state_summary: str
    logic_candidates: tuple[LogicCandidate, ...] = ()
    pressure: dict[int, float] = Field(default_factory=dict)
    expires_at_ms: int


class SpeakRequest(BaseEnvelope):
    type: Literal["speak_request"] = "speak_request"
    request_id: str
    phase_id: str
    npc_id: str
    seat_no: int
    logic_packet_id: str
    suggested_intent: str
    max_chars: int = 80
    max_duration_ms: int = 8000
    priority: int = 0
    expires_at_ms: int


class SpeakResult(BaseEnvelope):
    type: Literal["speak_result"] = "speak_result"
    request_id: str
    npc_id: str
    phase_id: str
    status: Literal["accepted", "declined", "error"]
    text: str | None = None
    used_logic_ids: tuple[str, ...] = ()
    intent: str | None = None
    estimated_duration_ms: int | None = None
    failure_reason: str | None = None


class PlaybackAuthorized(BaseEnvelope):
    type: Literal["playback_authorized"] = "playback_authorized"
    request_id: str
    npc_id: str
    status: Literal["authorized"] = "authorized"
    speech_event_id: str
    playback_deadline_ms: int


class PlaybackRejected(BaseEnvelope):
    type: Literal["playback_rejected"] = "playback_rejected"
    request_id: str
    npc_id: str
    status: Literal["rejected"] = "rejected"
    failure_reason: str


class TtsFinished(BaseEnvelope):
    type: Literal["tts_finished"] = "tts_finished"
    request_id: str
    npc_id: str
    tts_duration_ms: int
    audio_size_bytes: int


class TtsFailed(BaseEnvelope):
    type: Literal["tts_failed"] = "tts_failed"
    request_id: str
    npc_id: str
    failure_reason: str


class PlaybackFinished(BaseEnvelope):
    type: Literal["playback_finished"] = "playback_finished"
    request_id: str
    npc_id: str
    started_at_ms: int
    finished_at_ms: int


class PlaybackFailed(BaseEnvelope):
    type: Literal["playback_failed"] = "playback_failed"
    request_id: str
    npc_id: str
    failure_reason: str


# --------------------------------------------------- voice-ingest ↔ Master


class VadSpeechStarted(BaseEnvelope):
    type: Literal["vad_speech_started"] = "vad_speech_started"
    game_id: str
    phase_id: str
    speaker_discord_user_id: str
    seat_no: int
    segment_id: str
    audio_start_ms: int


class VadSpeechEnded(BaseEnvelope):
    type: Literal["vad_speech_ended"] = "vad_speech_ended"
    game_id: str
    phase_id: str
    speaker_discord_user_id: str
    seat_no: int
    segment_id: str
    audio_end_ms: int


class SpeechEventPayload(BaseEnvelope):
    type: Literal["speech_event_payload"] = "speech_event_payload"
    game_id: str
    phase_id: str
    seat_no: int
    speaker_discord_user_id: str
    segment_id: str
    text: str
    confidence: float
    duration_ms: int
    audio_start_ms: int
    audio_end_ms: int
    summary: str | None = None


class SttFailed(BaseEnvelope):
    type: Literal["stt_failed"] = "stt_failed"
    game_id: str
    phase_id: str
    speaker_discord_user_id: str
    seat_no: int
    segment_id: str
    failure_reason: str


class RegistrySnapshot(BaseEnvelope):
    type: Literal["registry_snapshot"] = "registry_snapshot"
    npc_user_ids: tuple[str, ...]


class RegistryUpdate(BaseEnvelope):
    type: Literal["registry_update"] = "registry_update"
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


# ---------------------------------------------------------------- Errors


class HandshakeError(BaseEnvelope):
    """Sent before connection close on PSK failure or malformed handshake."""

    type: Literal["handshake_error"] = "handshake_error"
    failure_reason: str


__all__ = [
    "BaseEnvelope",
    "HandshakeError",
    "Heartbeat",
    "LogicCandidate",
    "LogicPacket",
    "NpcRegister",
    "NpcRegistered",
    "PlaybackAuthorized",
    "PlaybackFailed",
    "PlaybackFinished",
    "PlaybackRejected",
    "RegistrySnapshot",
    "RegistryUpdate",
    "SpeakRequest",
    "SpeakResult",
    "SpeechEventPayload",
    "SttFailed",
    "TtsFailed",
    "TtsFinished",
    "VadSpeechEnded",
    "VadSpeechStarted",
]
