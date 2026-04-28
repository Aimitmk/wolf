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

from wolfbot.domain.enums import CoDeclaration


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


class SeatAssigned(BaseEnvelope):
    """Master → NPC: this NPC bot was picked for `game_id`. Join VC and
    stand by for SpeakRequests. Sent at phase entry after the registry
    pairs the bot with an LLM seat."""

    type: Literal["seat_assigned"] = "seat_assigned"
    npc_id: str
    seat_no: int
    game_id: str
    phase_id: str


class SeatReleased(BaseEnvelope):
    """Master → NPC: this NPC bot is no longer attached to a game. Leave
    VC and idle until the next `seat_assigned`. Sent on game end / host
    abort / explicit unassign so unselected bots don't linger in VC."""

    type: Literal["seat_released"] = "seat_released"
    npc_id: str
    game_id: str | None = None
    reason: str = "game_ended"


class SetMuteState(BaseEnvelope):
    """Master → NPC: set this bot's voice *self*-mute.

    Self-mute (vs. server-mute via ``Member.edit(mute=...)``) sidesteps
    both the MUTE_MEMBERS permission requirement and Discord's role
    hierarchy rule that the moderator's role must be strictly above the
    target. Each NPC owns its own voice state, so it can flip self-mute
    via gateway opcode without any admin power. Used to visually flag
    dead seats and non-discussion phases — viewers see a mic-muted icon
    next to dead/idle bots."""

    type: Literal["set_mute_state"] = "set_mute_state"
    npc_id: str
    self_mute: bool


class Heartbeat(BaseEnvelope):
    type: Literal["heartbeat"] = "heartbeat"
    npc_id: str | None = None  # populated by NPC bots; None for voice-ingest


class LogicCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    claim: str
    support: tuple[str, ...] = ()
    counter: tuple[str, ...] = ()


class RecentSpeech(BaseModel):
    """Compact rendering of a past public utterance for NPC context.

    Built from `SpeechEvent` rows by the SpeakArbiter and sent to the NPC
    bot as part of `LogicPacket.recent_speeches` so the NPC's prompt can
    surface recent dialogue (the parity gap with rounds-mode prompts that
    fed `PLAYER_SPEECH` log lines into `build_user_context`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seat_no: int
    display_name: str
    source: Literal["text", "voice_stt", "npc_generated"]
    text: str


class LogicPacket(BaseEnvelope):
    type: Literal["logic_packet"] = "logic_packet"
    packet_id: str
    phase_id: str
    recipient_npc_id: str
    public_state_summary: str
    logic_candidates: tuple[LogicCandidate, ...] = ()
    pressure: dict[int, float] = Field(default_factory=dict)
    expires_at_ms: int
    recent_speeches: tuple[RecentSpeech, ...] = Field(
        default_factory=tuple,
        description=(
            "Recent public utterances in the current phase, oldest-first. "
            "Defaults to empty so older Master builds talking to newer NPC "
            "bots stay schema-compatible."
        ),
    )


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
    role: str | None = Field(
        default=None,
        description=(
            "Role of the seat this NPC is bound to (VILLAGER / WEREWOLF / ...). "
            "Sent so the NPC's system prompt can surface role-specific "
            "strategy text. Optional for back-compat with older Master builds."
        ),
    )
    role_strategy: str | None = Field(
        default=None,
        description=(
            "Pre-rendered role-specific strategy markdown produced by Master's "
            "rounds-mode `build_strategy_block(role)`. Sent verbatim so the "
            "NPC bot doesn't need to import gameplay-LLM strategy data."
        ),
    )
    alive_seats: tuple[tuple[int, str], ...] = Field(
        default_factory=tuple,
        description=(
            "(seat_no, display_name) pairs for every still-alive seat. Lets "
            "the NPC's prompt list 'who is alive' without an extra round-trip. "
            "Empty for back-compat with older Master builds."
        ),
    )
    dead_seats: tuple[tuple[int, str], ...] = Field(
        default_factory=tuple,
        description=(
            "(seat_no, display_name) pairs for dead seats. Same back-compat "
            "story as `alive_seats`."
        ),
    )


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
    co_declaration: CoDeclaration | None = Field(
        default=None,
        description=(
            "Structured CO self-declaration tag set by the NPC's speech "
            "generator. Authoritative — Master persists it on SpeechEvent."
        ),
    )


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
    co_declaration: CoDeclaration | None = Field(
        default=None,
        description=(
            "Structured CO self-declaration extracted by Gemini's audio "
            "analyzer (`co_claim` field). Master persists it on SpeechEvent."
        ),
    )
    addressed_name: str | None = Field(
        default=None,
        description=(
            "Literal name/handle the speaker called out (e.g. 'セツ', "
            "'ジーナさん', '席3'). Master resolves this against the current "
            "seats table to populate SpeechEvent.addressed_seat_no. None when "
            "the analyzer didn't detect a named address."
        ),
    )


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
    "SeatAssigned",
    "SeatReleased",
    "SetMuteState",
    "SpeakRequest",
    "SpeakResult",
    "SpeechEventPayload",
    "SttFailed",
    "TtsFailed",
    "TtsFinished",
    "VadSpeechEnded",
    "VadSpeechStarted",
]
