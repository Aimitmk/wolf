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
from wolfbot.domain.suspicion import Suspicion


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
    past_votes: tuple[tuple[int, int, tuple[tuple[int, int | None], ...]], ...] = Field(
        default_factory=tuple,
        description=(
            "Public vote history for completed past days. Each entry is "
            "``(day, round, ((voter_seat, target_seat | None), ...))`` so "
            "the NPC prompt can render 「day1: 席1 → 席3, …」 without "
            "asking each NPC to remember its own ballot. Without this, "
            "models routinely fabricate a different vote target than the "
            "one they actually cast (observed live: ジナ said her vote "
            "was コメット when she actually voted セツ). Round 0 = main "
            "vote, round 1 = runoff."
        ),
    )
    pending_role_callouts: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Roles that some speaker has called out for in the current "
            "phase but no one has CO'd as yet (e.g. ``('seer',)`` after "
            "「占い師の方は名乗り出てください」). Real role holders should "
            "treat this as a CO trigger; wolf-side NPCs should consider "
            "whether to fake CO. Cleared on the Master side when a "
            "matching CO arrives."
        ),
    )
    past_suspicions: tuple[
        tuple[int, str, int, int, str, str, str | None, str | None], ...
    ] = Field(
        default_factory=tuple,
        description=(
            "Public suspicion timeline carried as a flat tuple of "
            "``(day, phase, suspecter_seat, target_seat, level, reason, "
            "update_from_level | None, update_reason | None)`` rows in "
            "chronological order. Used by the NPC speech LLM to render "
            "the immutable suspicion history block so silent reversals "
            "are detectable."
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
            "(seat_no, display_name) pairs for dead seats. Same back-compat story as `alive_seats`."
        ),
    )
    dead_seat_causes: tuple[tuple[int, str], ...] = Field(
        default_factory=tuple,
        description=(
            "Per-seat death cause tag for the dead_seats list, e.g. "
            "((2, 'EXECUTION'), (8, 'ATTACK')). Lets the NPC distinguish "
            "yesterday's vote victim from last night's attack victim "
            "without parsing the morning text. Optional — empty for "
            "back-compat with older Master builds; the NPC's prompt "
            "builder falls back to an unlabelled list when missing."
        ),
    )
    retry_feedback: str | None = Field(
        default=None,
        description=(
            "Master-side rejection note from a prior SpeakResult. Non-null "
            "only when Master detected a fabricated `claimed_seer_result` "
            "/ `claimed_medium_result` on the previous attempt and is "
            "asking the same NPC to retry. The NPC's prompt builder must "
            "surface this verbatim near the structured-output requirements "
            "so the model corrects its claim. Empty / None on first "
            "attempts and on non-fabrication retries."
        ),
    )


class ClaimedSeerResult(BaseModel):
    """Structured seer-CO result the NPC declares within a single utterance.

    Set on :class:`SpeakResult` when the NPC's `text` announces a new
    divination outcome — *real* (true seer) or *fake* (wolf/madman
    fake-seer-CO). Identical shape, different ground truth.

    Master persists every claim on the originating SpeechEvent and rolls
    them up into a per-seat "claim history" that gets injected back into
    every subsequent NPC prompt. That round-trip is what stops a fake
    seer from drifting between phases (Day1 「シゲミチ白」 → Day2 results
    that silently drop シゲミチ and add a fabricated コメット白).

    The `day` is implicit (= the speech's day field) and not carried here
    so the NPC can't accidentally mis-stamp a claim. ``target_name`` is
    likewise resolved Master-side from the seat lookup so the NPC never
    has to invent it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_seat: int = Field(ge=1, le=9)
    is_wolf: bool


class ClaimedMediumResult(BaseModel):
    """Structured medium-CO result the NPC declares within a single utterance.

    ``is_wolf`` may be ``None`` to encode the explicit "no execution
    yesterday → no result today" case so the NPC can claim the void
    rather than fabricating a result it doesn't have.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_seat: int = Field(ge=1, le=9)
    is_wolf: bool | None = None


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
    claimed_seer_result: ClaimedSeerResult | None = Field(
        default=None,
        description=(
            "Structured seer-CO result this utterance announces. Non-null "
            "iff the NPC's text describes a NEW divination outcome (real "
            "seer or fake-CO wolf/madman). Master persists it on the "
            "SpeechEvent and folds it into the per-seat claim history "
            "every subsequent prompt sees, anchoring fake seers to their "
            "prior lies."
        ),
    )
    claimed_medium_result: ClaimedMediumResult | None = Field(
        default=None,
        description=(
            "Structured medium-CO result this utterance announces. "
            "Identical handling to claimed_seer_result; ``is_wolf=null`` "
            "encodes the 'no execution yesterday' void case."
        ),
    )
    addressed_seat_no: int | None = Field(
        default=None,
        description=(
            "Legacy single-addressee field. When ``addressed_seat_nos`` "
            "is non-empty Master prefers the list and ignores this. "
            "Older NPC bot builds that don't know the list field still "
            "work via this fallback. None / unset for general remarks."
        ),
    )
    addressed_seat_nos: tuple[int, ...] = Field(
        default_factory=tuple,
        description=(
            "All seats this utterance is directed at — e.g. asking "
            "「セツとジナはどう思う?」 produces ``(2, 3)``. Master "
            "persists every entry on SpeechEvent so the arbiter "
            "prioritises every named NPC and consumes them one-by-one "
            "as each replies. Self-address (==speaker_seat) and "
            "non-alive seats are dropped on the Master boundary."
        ),
    )
    suspicions: tuple[Suspicion, ...] = Field(
        default_factory=tuple,
        description=(
            "Structured suspicion records this utterance asserts. Each "
            "entry says 「自分は target_seat を level (理由 reason) で見ている」. "
            "Master persists them to ``speech_suspicions`` keyed on "
            "``event_id`` and folds the immutable history into every "
            "subsequent prompt so a silent reversal (e.g. trust → high "
            "without setting ``update_from_level``) is detectable. "
            "Empty tuple is allowed but discouraged — see the speech "
            "system prompt's 名指し義務 rule."
        ),
    )


# ------------------------- Phase D: per-seat decision delegation
# Reactive_voice mode is migrating to "NPC bot is an embodied agent for its
# seat". The bot owns its role + private results in-memory and decides
# speech / vote / night-action via its own `NPC_LLM_*`. Master pushes the
# private state at /wolf start (and on NPC re-register) and asks for
# decisions when a phase needs them. None of these messages are required
# for back-compat — Master falls back to the historical Master-decides
# path (LLMAdapter / SpeakRequest) when the NPC bot is on an older build.


class SeerResult(BaseModel):
    """Past seer divination result a seer NPC needs to recall in speech."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: int
    target_seat: int
    target_name: str
    is_wolf: bool


class MediumResult(BaseModel):
    """Past medium post-execution result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: int
    target_seat: int
    target_name: str
    is_wolf: bool | None = None  # None when no execution happened


class GuardEntry(BaseModel):
    """Past knight guard target. ``peaceful_morning`` lets the NPC tell a
    truthful story about which guards led to which outcomes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: int
    target_seat: int
    target_name: str
    peaceful_morning: bool | None = None  # None until morning resolves


class WolfChatLine(BaseModel):
    """One line of the wolves-only chat history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: int
    speaker_seat: int
    speaker_name: str
    text: str


class WolfAttackEntry(BaseModel):
    """Past wolf attack target the wolves submitted on a given night.

    ``peaceful_morning`` is True when the attack was knight-GJ'd (no
    victim that morning), False when the target was killed, None when
    the morning hasn't resolved yet. Wolves use this to detect the
    "GJ → re-attack same target" pattern: the knight cannot guard the
    same seat on consecutive nights, so a GJ'd target is a free kill
    on the next night.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: int
    target_seat: int
    target_name: str
    peaceful_morning: bool | None = None


class PrivateStateSnapshot(BaseEnvelope):
    """Master → NPC: full private game state for the seat this NPC plays.

    Sent at game start (after roles assigned) and on NPC re-register so the
    NPC bot can rebuild its in-memory state without persisting anything to
    disk locally. Idempotent — the NPC replaces its state for that game_id
    with the snapshot's contents.
    """

    type: Literal["private_state_snapshot"] = "private_state_snapshot"
    npc_id: str
    game_id: str
    seat_no: int
    persona_key: str
    role: str  # canonical Role enum value (VILLAGER / WEREWOLF / ...)
    day_number: int = 0
    alive_seats: tuple[tuple[int, str], ...] = ()
    dead_seats: tuple[tuple[int, str], ...] = ()
    dead_seat_causes: tuple[tuple[int, str], ...] = ()
    # Wolf-only — empty for non-wolves.
    partner_wolves: tuple[tuple[int, str], ...] = ()
    # Role-specific result history. Empty for roles without these powers.
    seer_results: tuple[SeerResult, ...] = ()
    medium_results: tuple[MediumResult, ...] = ()
    guard_history: tuple[GuardEntry, ...] = ()
    wolf_chat_history: tuple[WolfChatLine, ...] = ()
    wolf_attack_history: tuple[WolfAttackEntry, ...] = ()


class PrivateStateUpdate(BaseEnvelope):
    """Master → NPC: incremental update to the seat's private state.

    Push semantics, append-only on the NPC's local state. ``payload`` shape
    depends on ``update_kind``:

    * ``seer_result`` → SeerResult-shaped fields
    * ``medium_result`` → MediumResult-shaped fields
    * ``guard_entry`` → GuardEntry-shaped fields
    * ``guard_resolved`` → ``{"day": int, "peaceful_morning": bool}`` —
      retroactively fills the previous night's `peaceful_morning` flag.
    * ``wolf_chat`` → WolfChatLine-shaped fields
    * ``alive_changed`` → ``{"alive_seats": [...], "dead_seats": [...]}``
    * ``day_advanced`` → ``{"day_number": int}``
    """

    type: Literal["private_state_update"] = "private_state_update"
    npc_id: str
    game_id: str
    seat_no: int
    update_kind: Literal[
        "seer_result",
        "medium_result",
        "guard_entry",
        "guard_resolved",
        "wolf_chat",
        "alive_changed",
        "day_advanced",
    ]
    payload: dict[str, object] = Field(default_factory=dict)


class DecideVoteRequest(BaseEnvelope):
    """Master → NPC: decide a vote target for ``round_`` (0=regular, 1=runoff).

    ``candidate_seats`` is the legal target set the state machine produced
    (alive seats minus self). NPC may return ``target_seat=None`` to
    abstain.
    """

    type: Literal["decide_vote_request"] = "decide_vote_request"
    request_id: str
    npc_id: str
    seat_no: int
    game_id: str
    phase_id: str
    round_: int = 0
    candidate_seats: tuple[tuple[int, str], ...]
    public_state_summary: str = ""
    expires_at_ms: int


class VoteDecision(BaseEnvelope):
    """NPC → Master: vote target choice. ``target_seat=None`` = abstain."""

    type: Literal["vote_decision"] = "vote_decision"
    request_id: str
    npc_id: str
    seat_no: int
    target_seat: int | None
    reason_summary: str = ""


class DecideNightActionRequest(BaseEnvelope):
    """Master → NPC: decide a night action target.

    ``action_kind`` determines the seat universe: wolf_attack / seer_divine
    / knight_guard. ``candidate_seats`` is the legal target set.
    """

    type: Literal["decide_night_action_request"] = "decide_night_action_request"
    request_id: str
    npc_id: str
    seat_no: int
    game_id: str
    phase_id: str
    action_kind: Literal["wolf_attack", "seer_divine", "knight_guard"]
    candidate_seats: tuple[tuple[int, str], ...]
    public_state_summary: str = ""
    expires_at_ms: int


class NightActionDecision(BaseEnvelope):
    """NPC → Master: night-action target choice. ``target_seat=None`` =
    skip. Master writes the action to ``night_actions`` and applies the
    rules at phase resolution."""

    type: Literal["night_action_decision"] = "night_action_decision"
    request_id: str
    npc_id: str
    seat_no: int
    action_kind: Literal["wolf_attack", "seer_divine", "knight_guard"]
    target_seat: int | None
    reason_summary: str = ""


class WolfChatRequest(BaseEnvelope):
    """Master → NPC (wolf seat only): "post a coordination line now".

    Sent sequentially to each alive wolf NPC at the start of the night
    phase before attack-decision dispatch. Each wolf reads the others'
    `wolf_chat_history` (already updated via `private_state_update`) so
    the chain converges on a target. NPC replies via `WolfChatSend`
    carrying the same `request_id` so Master can drain its pending
    futures.
    """

    type: Literal["wolf_chat_request"] = "wolf_chat_request"
    request_id: str
    npc_id: str
    seat_no: int
    game_id: str
    phase_id: str
    candidate_seats: tuple[tuple[int, str], ...] = Field(
        default_factory=tuple,
        description=(
            "(seat_no, name) pairs of legal attack targets — passed so the "
            "wolf NPC can ground its proposal in real candidates rather "
            "than improvising a name."
        ),
    )
    public_state_summary: str = ""
    expires_at_ms: int


class WolfChatSend(BaseEnvelope):
    """NPC (wolf seat only) → Master: post a line to the wolves' private
    chat. Master persists it as a `WOLF_CHAT` private LogEntry and pushes
    a `wolf_chat` PrivateStateUpdate to every other live wolf seat's NPC.

    `request_id` is non-null when the line was prompted by a
    `WolfChatRequest` from Master; the dispatcher uses it to resolve the
    pending future. Spontaneous wolf chat (a wolf NPC volunteers a line
    without a request) leaves it null — Master broker still persists +
    fans out, just without resolving any future.
    """

    type: Literal["wolf_chat_send"] = "wolf_chat_send"
    npc_id: str
    seat_no: int
    game_id: str
    text: str
    request_id: str | None = None


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
    addressed_seat_no: int | None = Field(
        default=None,
        description=(
            "Seat number the analyzer pre-resolved ``addressed_name`` to "
            "when its prompt was grounded with a roster. Master prefers "
            "this over running ``resolve_seat_by_name(addressed_name, ...)`` "
            "which only matches against the persona-canonical "
            "``Seat.display_name`` and fails when the bot's live VC "
            "nickname diverges from the persona handle. None when the "
            "analyzer wasn't grounded or couldn't pick a seat."
        ),
    )
    role_callout: CoDeclaration | None = Field(
        default=None,
        description=(
            'Role the utterance is calling out for (e.g. "占い師いますか?" '
            "→ ``seer``). Mirrors the human-side STT field so wolf-side "
            "NPCs and real role holders can react. None for the vast "
            "majority of utterances; only set when the analyzer detects "
            "an explicit role-callout intent (not mere mentions of a "
            "role name in unrelated context)."
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
    "ClaimedMediumResult",
    "ClaimedSeerResult",
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
