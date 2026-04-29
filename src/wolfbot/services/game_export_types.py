"""Pydantic models defining the canonical viewer export schema.

This module is the single source of truth for the per-game export shape.
It is mirrored in ``viewer/src/lib/types.ts`` (TypeScript types) and
``viewer/sample-data/export-schema.json`` (JSON Schema, regenerated from
:class:`GameExport` via ``scripts/dump-export-schema.py``).

Contract:

* :func:`wolfbot.services.game_export.export_game` constructs and writes
  a :class:`GameExport` — never a bare ``dict[str, Any]``.
* The viewer validates each loaded JSON against ``export-schema.json``
  at test time; any drift fails the viewer's contract test.
* A drift test on the Python side
  (:mod:`tests.test_game_export_integration`) asserts the committed
  schema file matches what :meth:`GameExport.model_json_schema` emits.

Time fields exposed to the viewer are uniformly milliseconds since epoch.
The DB stores ``created_at`` / ``submitted_at`` in seconds; the exporter
multiplies by 1000 at the boundary, before it builds these models.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from wolfbot.domain.enums import CoDeclaration as _CoDeclaration

# A frozen, closed shape — extra fields cause validation errors so a
# typo or a stale field caught here rather than silently dropped.
_StrictConfig = ConfigDict(frozen=True, extra="forbid")
# Trace entries come from JSONL files written by `log_llm_call`. Future
# versions may add metadata keys; allow extras so older viewers don't
# refuse to parse newer traces. Required keys are still validated.
_TraceConfig = ConfigDict(frozen=True, extra="allow")


RoleKey = Literal["VILLAGER", "WEREWOLF", "MADMAN", "SEER", "MEDIUM", "KNIGHT"]
DeathCause = Literal["EXECUTION", "ATTACK"]
DiscussionMode = Literal["rounds", "reactive_voice"]
# Mirrors the production `wolfbot.domain.discussion.SpeechSource` minus
# the internal `phase_baseline` sentinel (filtered out by the exporter —
# it's a private state-rebuild marker, not a viewable utterance).
SpeechSource = Literal["text", "voice_stt", "npc_generated"]
# Re-export the canonical wire/storage form from domain/enums so the
# viewer schema stays aligned with runtime validators in lockstep.
CoDeclaration = _CoDeclaration
TraceRole = Literal["gameplay", "npc_speech", "voice_stt", "text_analysis"]
Victory = Literal["village", "wolf"]


class GameMeta(BaseModel):
    model_config = _StrictConfig

    id: str
    guild_id: str
    host_user_id: str
    discussion_mode: DiscussionMode
    created_at_ms: int
    ended_at_ms: int | None
    victory: Victory | None
    main_text_channel_id: str
    main_vc_channel_id: str


class SeatExport(BaseModel):
    model_config = _StrictConfig

    seat_no: int
    display_name: str
    is_llm: bool
    persona_key: str | None
    discord_user_id: str | None
    role: RoleKey
    alive: bool
    death_cause: DeathCause | None
    death_day: int | None


class PublicLogEntry(BaseModel):
    model_config = _StrictConfig

    kind: str
    actor_seat: int | None
    text: str
    created_at_ms: int


class SpeechEventExport(BaseModel):
    model_config = _StrictConfig

    event_id: str
    source: SpeechSource
    speaker_seat: int | None
    text: str
    stt_confidence: float | None
    summary: str | None
    co_declaration: CoDeclaration | None
    addressed_seat_no: int | None
    created_at_ms: int


class VoteExport(BaseModel):
    model_config = _StrictConfig

    day: int
    round: int
    voter_seat: int
    target_seat: int | None
    submitted_at_ms: int


class NightActionExport(BaseModel):
    model_config = _StrictConfig

    day: int
    actor_seat: int
    kind: str
    target_seat: int | None
    submitted_at_ms: int


class PhaseSection(BaseModel):
    model_config = _StrictConfig

    day: int
    phase: str
    started_at_ms: int
    public_logs: list[PublicLogEntry]
    speech_events: list[SpeechEventExport]
    votes: list[VoteExport]
    night_actions: list[NightActionExport]


class TokenUsage(BaseModel):
    model_config = _StrictConfig

    prompt: int | None
    completion: int | None
    total: int | None


class TraceEntry(BaseModel):
    """One JSONL trace row — see :mod:`wolfbot.services.llm_trace`.

    ``extra="allow"`` because trace metadata is intentionally open: the
    Master / NPC paths may attach arbitrary debug fields that the viewer
    just renders verbatim.
    """

    model_config = _TraceConfig

    ts: str
    role: TraceRole
    provider: str
    model: str
    phase: str | None
    day: int | None
    actor: str | None
    system_prompt: str | None
    user_prompt: str | None
    response: str | None
    latency_ms: int
    tokens: TokenUsage | None
    error: str | None
    metadata: dict[str, Any] | None = None
    file_stem: str | None = None


class ArbiterDecisionEntry(BaseModel):
    """One Master-side `SpeakRequest` dispatch — the "why this NPC, why now"
    breadcrumb the viewer surfaces alongside the resulting NPC speech.

    Joined from three persistence rows keyed by ``request_id``:

    * ``npc_speak_requests`` — the dispatch itself (Master → NPC)
    * ``npc_speak_results`` — NPC's reply (accepted / rejected / failed)
    * ``npc_playback_events`` — TTS + Discord playback outcome

    Any of the three may be missing for an in-flight or interrupted
    request; only ``request_id`` / ``phase_id`` / ``npc_id`` / ``seat_no``
    / ``created_at_ms`` are guaranteed.
    """

    model_config = _StrictConfig

    request_id: str
    phase_id: str
    npc_id: str
    seat_no: int
    suggested_intent: str
    selection_reason: str | None
    public_state_snapshot: dict[str, Any] | None
    logic_packet_id: str
    created_at_ms: int
    expires_at_ms: int

    # speak_results join (None if NPC never replied / TTL expired)
    result_status: str | None = None
    result_text: str | None = None
    result_intent: str | None = None
    result_failure_reason: str | None = None
    result_received_at_ms: int | None = None

    # playback_events join (None if request was rejected before TTS)
    playback_outcome: str | None = None
    playback_failure_reason: str | None = None
    playback_finished_at_ms: int | None = None
    tts_outcome: str | None = None
    tts_duration_ms: int | None = None


class GameExport(BaseModel):
    """Top-level shape of one ``viewer/games/{id}.json`` file."""

    model_config = _StrictConfig

    game: GameMeta
    seats: list[SeatExport]
    phases: list[PhaseSection]
    trace: list[TraceEntry]
    arbiter_decisions: list[ArbiterDecisionEntry] = []


__all__ = [
    "ArbiterDecisionEntry",
    "CoDeclaration",
    "DeathCause",
    "DiscussionMode",
    "GameExport",
    "GameMeta",
    "NightActionExport",
    "PhaseSection",
    "PublicLogEntry",
    "RoleKey",
    "SeatExport",
    "SpeechEventExport",
    "SpeechSource",
    "TokenUsage",
    "TraceEntry",
    "TraceRole",
    "Victory",
    "VoteExport",
]
