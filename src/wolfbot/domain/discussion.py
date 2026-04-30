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
            "Legacy single-addressee field. When ``addressed_seat_nos`` is "
            "set this mirrors its first element so older readers (DB row "
            "writers, restart recovery for pre-multi-address events) keep "
            "working. Prefer ``addressed_seat_nos`` for new code."
        ),
    )
    addressed_seat_nos: tuple[int, ...] = Field(
        default_factory=tuple,
        description=(
            "All seats this utterance is addressed to. SpeakArbiter "
            "prioritises every seat in the set on the next dispatch and "
            "consumes them one-by-one as each addressee replies. Empty "
            "for general remarks. The legacy `addressed_seat_no` field "
            "carries the first element for back-compat with code that "
            "only knows the singular form. Use "
            ":func:`event_addressed_seats` rather than reading either "
            "field directly so the singular-only fallback is applied "
            "consistently."
        ),
    )
    role_callout: str | None = Field(
        default=None,
        description=(
            "Role the utterance is calling out for "
            "('占い師の方どうぞ' → 'seer'). Stored alongside the speech so "
            "the arbiter and NPC prompt builders can surface a 'pending "
            "role callout' to every NPC: real role holders take it as a "
            "CO trigger, wolf-side NPCs take it as a chance to fake CO. "
            "Distinct from `co_declaration` (= self-declaration). Values "
            "are the canonical CoDeclaration enum strings."
        ),
    )
    claimed_seer_target_seat: int | None = Field(
        default=None,
        ge=1,
        le=9,
        description=(
            "Target seat the speaker claimed to have divined in this "
            "utterance — *real* (true seer) or *fake* (wolf/madman). Set "
            "iff the speech announces a NEW divination outcome; null "
            "otherwise (general remarks, mere references to prior "
            "results). Pairs with ``claimed_seer_is_wolf``. "
            "Authoritative source for the per-seat claim history that "
            "Master folds back into every subsequent prompt."
        ),
    )
    claimed_seer_is_wolf: bool | None = Field(
        default=None,
        description=(
            "Black/white verdict the speaker attached to "
            "``claimed_seer_target_seat``. None when no seer claim was "
            "made this utterance."
        ),
    )
    claimed_medium_target_seat: int | None = Field(
        default=None,
        ge=1,
        le=9,
        description=(
            "Target seat (= yesterday's executed seat) the speaker "
            "claimed a medium result for. Mirror of the seer fields for "
            "medium-CO."
        ),
    )
    claimed_medium_is_wolf: bool | None = Field(
        default=None,
        description=(
            "Black/white verdict for the medium claim. None when "
            "``claimed_medium_target_seat`` is None *or* when the "
            "speaker explicitly declared 'no execution yesterday → no "
            "result'."
        ),
    )
    created_at_ms: int

    def is_baseline(self) -> bool:
        return self.source == SpeechSource.PHASE_BASELINE


def event_addressed_seats(event: SpeechEvent) -> tuple[int, ...]:
    """Return the canonical ordered list of addressees for a SpeechEvent.

    Single source of truth: prefer ``addressed_seat_nos`` (the new
    multi-addressee field) and fall back to wrapping the legacy
    ``addressed_seat_no`` in a 1-tuple so test fixtures and older
    callers that only set the singular field still feed the fold
    correctly. Returns ``()`` when neither is set.
    """
    if event.addressed_seat_nos:
        return event.addressed_seat_nos
    if event.addressed_seat_no is not None:
        return (event.addressed_seat_no,)
    return ()


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
    last_addressed_seats: frozenset[int] = frozenset()
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
    # Set of role names ("seer" / "medium" / "knight") that some speaker
    # has explicitly called for in this phase but no one has CO'd as yet.
    # Cleared when a matching CO arrives (= the call was answered) and
    # reset per phase. Surfaces in the NPC prompt so real role holders
    # take it as a CO trigger and wolf-side NPCs can decide whether to
    # fake CO.
    pending_role_callouts: frozenset[str] = frozenset()
    # Roles whose *first ever* CO just landed in this game. Triggers the
    # arbiter's counter-CO opportunity pool: the next dispatches go to
    # the real role-holder (when uncpd) plus every wolf-side seat that
    # hasn't claimed any info role yet, in random order, one chance
    # each. Each member's reply (CO or skip) consumes their slot via
    # the arbiter's `_callout_pool_asked` tracker. The pool exhausts
    # when every member has been asked, after which normal priority
    # resumes — without this window, the village-side first-CO often
    # got "free real" treatment because no wolf had a guaranteed turn
    # to contest it before the discussion drifted onto other topics.
    pending_co_response: frozenset[str] = frozenset()
    # Per-seat utterance count within this phase (non-baseline events
    # only). The arbiter prefers seats with the lowest count so a
    # talkative NPC doesn't monopolize the phase — the binary
    # ``silent_seats`` was indistinguishable once every seat had spoken
    # once, after which the rotation collapsed to seat-number tiebreak
    # and the lowest-seat NPC kept winning. ``speech_counts`` extends
    # that signal across the entire phase so wolf-side NPCs at higher
    # seat numbers still get fair speaking turns (= more chances to
    # fake-CO).
    speech_counts: dict[int, int] = Field(default_factory=dict)
