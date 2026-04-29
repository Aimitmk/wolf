"""Public discussion domain — SpeechEvent + PublicDiscussionState.

Frozen Pydantic models that describe public utterances (human voice via STT,
human text, NPC-generated text) plus a sentinel `phase_baseline` row that
seeds the alive-seat baseline at the start of every public speech phase.

Pure: no I/O, no asyncio.

The `SpeechEvent` row is the unified public-utterance contract. Both
`LLM_DISCUSSION_MODE` modes (`rounds` and `reactive_voice`) write SpeechEvent
rows for every utterance, so persistence and downstream telemetry are mode-
independent. The `phase_baseline` sentinel makes the `PublicDiscussionState`
fold self-contained — the rebuild path reads only `speech_events`, never the
`seats` table — which is critical on Master restart.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from wolfbot.domain.enums import Phase


class SpeechSource(StrEnum):
    """Origin of a `SpeechEvent` row.

    - `voice_stt`: human voice transcribed by voice-ingest's STT.
    - `text`: human Discord text message captured by Master's main-channel listener.
    - `npc_generated`: utterance produced by an LLM-controlled NPC seat.
    - `phase_baseline`: sentinel inserted at the start of every public speech phase
      to record the alive-seat set; consumed by `PublicDiscussionState` rebuild.
      Excluded from public-log emission, channel posts, and downstream counts.
    """

    VOICE_STT = "voice_stt"
    TEXT = "text"
    NPC_GENERATED = "npc_generated"
    PHASE_BASELINE = "phase_baseline"


class SpeakerKind(StrEnum):
    HUMAN = "human"
    NPC = "npc"
    SYSTEM = "system"  # only used by the phase_baseline sentinel


class SpeechEvent(BaseModel):
    """A public utterance recorded by Master.

    Frozen (`ConfigDict(frozen=True)`) — never mutate after construction. Persisted
    one-to-one with rows in the `speech_events` table.

    Fields specific to each `source`:
      - `voice_stt`:        `stt_confidence`, `audio_start_ms`, `audio_end_ms` populated;
                            `alive_seat_nos_json` NULL.
      - `text`:             `stt_confidence` / `audio_*` NULL; `alive_seat_nos_json` NULL.
      - `npc_generated`:    `stt_confidence` / `audio_*` NULL; `alive_seat_nos_json` NULL.
      - `phase_baseline`:   `text` is "" or a short marker; `speaker_seat` is None;
                            `speaker_kind = SpeakerKind.SYSTEM`; `alive_seat_nos_json`
                            populated with the JSON-encoded list of alive seat numbers.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str
    game_id: str
    phase_id: str
    day: int = Field(ge=0)
    phase: Phase
    source: SpeechSource
    speaker_kind: SpeakerKind
    speaker_seat: int | None = Field(default=None, ge=1, le=9)
    text: str
    stt_confidence: float | None = None
    audio_start_ms: int | None = None
    audio_end_ms: int | None = None
    summary: str | None = None
    alive_seat_nos_json: str | None = None
    co_declaration: str | None = Field(
        default=None,
        description=(
            "Structured CO self-declaration tag (`seer` / `medium` / `knight`) "
            "extracted at the source: schema field for NPC/LLM speech, "
            "Gemini's `co_claim` for human voice. Authoritative when set; "
            "legacy events fall back to `_CO_MARKERS` substring scan."
        ),
    )
    addressed_seat_no: int | None = Field(
        default=None,
        ge=1,
        le=9,
        description=(
            "Seat number this utterance is addressed to "
            "('〜さん、どう思う' style direct address). "
            "Resolved on Master from the analyzer's `addressed_name` against the "
            "current seats table. SpeakArbiter prefers this NPC when picking "
            "the next speaker."
        ),
    )
    created_at_ms: int

    def is_baseline(self) -> bool:
        return self.source == SpeechSource.PHASE_BASELINE


def make_phase_id(game_id: str, day: int, phase: Phase, sequence: int = 1) -> str:
    """Canonical `phase_id` format used across the master / voice-ingest / NPC bots.

    Example: ``g_abc::day1::DAY_DISCUSSION::1``. The `sequence` lets a single
    `(game, day, phase)` triple have multiple phase_ids if a runoff causes the
    discussion phase to be re-entered conceptually.
    """
    return f"{game_id}::day{day}::{phase.value}::{sequence}"


class CoClaim(BaseModel):
    """A single CO-style self-declaration captured from `SpeechEvent.text`.

    The `role_claim` follows canonical role keywords (`seer`, `medium`, `knight`).
    """

    model_config = ConfigDict(frozen=True)

    seat: int = Field(ge=1, le=9)
    role_claim: str
    declared_at_event_id: str


class PublicDiscussionState(BaseModel):
    """Code-derived view of the live public discussion.

    Built deterministically from `SpeechEvent` rows of a single `phase_id`.
    Never written to disk in MVP; rebuilt from the event log on Master restart.
    The `phase_baseline` sentinel provides the `alive_seat_nos` baseline that
    `silent_seats` is computed against — so the fold needs no access to the
    `seats` table.
    """

    model_config = ConfigDict(frozen=False)

    game_id: str
    phase_id: str
    day: int
    alive_seat_nos: frozenset[int] = frozenset()
    co_claims: tuple[CoClaim, ...] = ()
    stances: dict[int, dict[int, float]] = Field(default_factory=dict)
    pressure: dict[int, float] = Field(default_factory=dict)
    open_topics: tuple[str, ...] = ()
    silent_seats: frozenset[int] = frozenset()
    recent_speech_event_ids: tuple[str, ...] = ()
    last_addressed_seat: int | None = None
    last_addressed_speaker_seat: int | None = None
    last_addressed_text: str = ""
    # Most recent non-sentinel speech_event speaker. SpeakArbiter uses this
    # as a de-prioritization signal for the *next* dispatch so that once
    # `silent_seats` empties (= every alive NPC has spoken once), the
    # rotation can't re-pick the just-finished speaker. Without this, the
    # arbiter falls back to seat-number tiebreak and seat 1 monopolizes
    # the rest of the phase.
    last_speaker_seat: int | None = None
    # Sliding window of `(speaker_seat, has_info)` for the most recent
    # non-sentinel speech events in this phase. ``has_info`` is True when
    # the event added structured information (currently: a CO declaration;
    # future signals can be added without changing the field shape).
    # SpeakArbiter consumes this to detect a low-information pair volley
    # (e.g. ラキオ ↔ ジョナス pinging each other for several rounds without
    # CO or new accusation target) and demote both seats so a third NPC
    # gets picked instead. Capped at 6 entries so the window stays small
    # but big enough to cover both the pair-volley check (last 4) and the
    # consecutive-speaker cap (last 3).
    recent_speech_summary: tuple[tuple[int, bool], ...] = ()
