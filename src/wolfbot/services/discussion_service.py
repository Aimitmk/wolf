"""SpeechEvent persistence + write hooks for `PLAYER_SPEECH` log emission.

This module is the on-Master ingestion seam for every public utterance,
regardless of source. The contract:

    write(SpeechEvent) → INSERT speech_events row
                       → emit PLAYER_SPEECH `LogEntry`
                       → post to main text channel via MessagePoster
                       (both sub-effects skipped per source — see below)

`source = phase_baseline` rows are sentinels; they receive no LogEntry and no
channel post, and are filtered out of every downstream consumer count.

`source = text` rows already exist in Discord as the player's original main-
channel message, so the channel post is skipped to avoid duplication. The
`PLAYER_SPEECH` LogEntry is still emitted so post-game replay sees a single
canonical timeline regardless of source.

`source ∈ {voice_stt, npc_generated}` rows always emit both the LogEntry and
the channel post — these utterances do NOT yet exist in the main channel and
need to be visible to text-only observers.

The Sqlite implementation reuses `SqliteRepo`'s open connection so writes
serialize naturally with the rest of Master's state.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Iterable, Sequence
from typing import Any, Protocol, runtime_checkable

import aiosqlite

from wolfbot.domain.discussion import (
    CoClaim,
    PublicDiscussionState,
    SpeakerKind,
    SpeechEvent,
    SpeechSource,
    event_addressed_seats,
    make_phase_id,
)
from wolfbot.domain.enums import CO_CLAIM_VALUES, Phase
from wolfbot.domain.models import LogEntry

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------- Protocols


@runtime_checkable
class SpeechEventStore(Protocol):
    """Persistence surface for `SpeechEvent`. Tests substitute an in-memory fake."""

    async def insert(self, event: SpeechEvent) -> None: ...

    async def load_phase(self, game_id: str,
                         phase_id: str) -> Sequence[SpeechEvent]: ...

    async def load_for_game(self, game_id: str) -> Sequence[SpeechEvent]: ...


@runtime_checkable
class SpeechMessagePoster(Protocol):
    """Subset of DiscordBotAdapter used by the write hook for main-channel posts."""

    async def post_public(self, game_id: str, text: str,
                          kind: str) -> None: ...


@runtime_checkable
class PublicLogSink(Protocol):
    """`SqliteRepo.insert_log_public` shape, decoupled for testability."""

    async def insert_log_public(self, entry: LogEntry) -> None: ...


# ---------------------------------------------------------------------- Sqlite store


class SqliteSpeechEventStore:
    """SQLite-backed `SpeechEventStore`. Reuses an existing aiosqlite connection."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert(self, event: SpeechEvent) -> None:
        nos_json: str | None = None
        if event.addressed_seat_nos:
            nos_json = json.dumps(list(event.addressed_seat_nos))
        await self._conn.execute(
            """
            INSERT INTO speech_events (
                event_id, game_id, phase_id, day, phase, source, speaker_kind,
                speaker_seat, text, stt_confidence, audio_start_ms, audio_end_ms,
                alive_seat_nos_json, summary, co_declaration, addressed_seat_no,
                addressed_seat_nos_json, role_callout,
                claimed_seer_target_seat, claimed_seer_is_wolf,
                claimed_medium_target_seat, claimed_medium_is_wolf,
                created_at_ms
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
            """,
            (
                event.event_id,
                event.game_id,
                event.phase_id,
                event.day,
                event.phase.value,
                event.source.value,
                event.speaker_kind.value,
                event.speaker_seat,
                event.text,
                event.stt_confidence,
                event.audio_start_ms,
                event.audio_end_ms,
                event.alive_seat_nos_json,
                event.summary,
                event.co_declaration,
                event.addressed_seat_no,
                nos_json,
                event.role_callout,
                event.claimed_seer_target_seat,
                _bool_to_int(event.claimed_seer_is_wolf),
                event.claimed_medium_target_seat,
                _bool_to_int(event.claimed_medium_is_wolf),
                event.created_at_ms,
            ),
        )
        await self._conn.commit()

    async def load_phase(self, game_id: str, phase_id: str) -> Sequence[SpeechEvent]:
        async with self._conn.execute(
            """
            SELECT event_id, game_id, phase_id, day, phase, source, speaker_kind,
                   speaker_seat, text, stt_confidence, audio_start_ms, audio_end_ms,
                   alive_seat_nos_json, summary, co_declaration, addressed_seat_no,
                   addressed_seat_nos_json, role_callout,
                   claimed_seer_target_seat, claimed_seer_is_wolf,
                   claimed_medium_target_seat, claimed_medium_is_wolf,
                   created_at_ms
              FROM speech_events
             WHERE game_id=? AND phase_id=?
             ORDER BY created_at_ms ASC, event_id ASC
            """,
            (game_id, phase_id),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_event(row) for row in rows]

    async def load_for_game(self, game_id: str) -> Sequence[SpeechEvent]:
        async with self._conn.execute(
            """
            SELECT event_id, game_id, phase_id, day, phase, source, speaker_kind,
                   speaker_seat, text, stt_confidence, audio_start_ms, audio_end_ms,
                   alive_seat_nos_json, summary, co_declaration, addressed_seat_no,
                   addressed_seat_nos_json, role_callout,
                   claimed_seer_target_seat, claimed_seer_is_wolf,
                   claimed_medium_target_seat, claimed_medium_is_wolf,
                   created_at_ms
              FROM speech_events
             WHERE game_id=?
             ORDER BY created_at_ms ASC, event_id ASC
            """,
            (game_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_event(row) for row in rows]


def _bool_to_int(value: bool | None) -> int | None:
    """SQLite has no native boolean — keep the 0/1/NULL convention used
    elsewhere in the schema (peaceful_morning, force_skip_pending, etc.)
    so the column reads cleanly with `WHERE claimed_seer_is_wolf = 1`.
    """
    if value is None:
        return None
    return 1 if value else 0


def _int_to_bool(value: int | None) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _row_to_event(row: Any) -> SpeechEvent:
    nos_json = row[16]
    addressed_nos: tuple[int, ...] = ()
    if nos_json:
        try:
            parsed = json.loads(nos_json)
            if isinstance(parsed, list):
                addressed_nos = tuple(int(s) for s in parsed if s is not None)
        except (json.JSONDecodeError, ValueError, TypeError):
            addressed_nos = ()
    # Legacy events written before addressed_seat_nos_json existed: synth
    # the tuple from the singular column so the fold sees both forms
    # consistently.
    if not addressed_nos and row[15] is not None:
        addressed_nos = (int(row[15]),)
    return SpeechEvent(
        event_id=row[0],
        game_id=row[1],
        phase_id=row[2],
        day=row[3],
        phase=Phase(row[4]),
        source=SpeechSource(row[5]),
        speaker_kind=SpeakerKind(row[6]),
        speaker_seat=row[7],
        text=row[8],
        stt_confidence=row[9],
        audio_start_ms=row[10],
        audio_end_ms=row[11],
        alive_seat_nos_json=row[12],
        summary=row[13],
        co_declaration=row[14],
        addressed_seat_no=row[15],
        addressed_seat_nos=addressed_nos,
        role_callout=row[17],
        claimed_seer_target_seat=row[18],
        claimed_seer_is_wolf=_int_to_bool(row[19]),
        claimed_medium_target_seat=row[20],
        claimed_medium_is_wolf=_int_to_bool(row[21]),
        created_at_ms=row[22],
    )


# ---------------------------------------------------------------------- Helpers


def new_event_id() -> str:
    """ULID-shaped opaque id; uuid4 hex is acceptable for MVP uniqueness."""
    return uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


def make_phase_baseline(
    *,
    game_id: str,
    phase_id: str,
    day: int,
    phase: Phase,
    alive_seat_nos: Iterable[int],
    created_at_ms: int | None = None,
) -> SpeechEvent:
    """Build the sentinel SpeechEvent inserted at the start of every public speech phase.

    Per accepted spec conflict AC2, the sentinel records the alive-seat baseline
    so `PublicDiscussionState` rebuild reads only `speech_events` (never `seats`).
    Consumers MUST filter `source != phase_baseline` for human-facing counts.
    """
    seats_sorted = sorted(set(alive_seat_nos))
    return SpeechEvent(
        event_id=new_event_id(),
        game_id=game_id,
        phase_id=phase_id,
        day=day,
        phase=phase,
        source=SpeechSource.PHASE_BASELINE,
        speaker_kind=SpeakerKind.SYSTEM,
        speaker_seat=None,
        text="",
        stt_confidence=None,
        audio_start_ms=None,
        audio_end_ms=None,
        alive_seat_nos_json=json.dumps(seats_sorted),
        created_at_ms=created_at_ms if created_at_ms is not None else now_ms(),
    )


def _normalize_addressed(
    addressed_seat_no: int | None,
    addressed_seat_nos: tuple[int, ...] | None,
) -> tuple[int | None, tuple[int, ...]]:
    """Coerce the singular + list form into a consistent pair.

    Returns ``(seat_no, seat_nos)`` where ``seat_nos`` is the canonical
    list and ``seat_no`` mirrors its first element. Either input may be
    None / empty; if both are set, the list wins and ``seat_no`` is
    ignored. Removes duplicates while preserving order.
    """
    nos: list[int] = []
    if addressed_seat_nos:
        for s in addressed_seat_nos:
            if s is None:
                continue
            if s not in nos:
                nos.append(int(s))
    if not nos and addressed_seat_no is not None:
        nos.append(int(addressed_seat_no))
    if not nos:
        return (None, ())
    return (nos[0], tuple(nos))


def make_human_text_event(
    *,
    game_id: str,
    phase_id: str,
    day: int,
    phase: Phase,
    speaker_seat: int,
    text: str,
    co_declaration: str | None = None,
    addressed_seat_no: int | None = None,
    addressed_seat_nos: tuple[int, ...] | None = None,
    role_callout: str | None = None,
    created_at_ms: int | None = None,
) -> SpeechEvent:
    seat_no, seat_nos = _normalize_addressed(addressed_seat_no, addressed_seat_nos)
    return SpeechEvent(
        event_id=new_event_id(),
        game_id=game_id,
        phase_id=phase_id,
        day=day,
        phase=phase,
        source=SpeechSource.TEXT,
        speaker_kind=SpeakerKind.HUMAN,
        speaker_seat=speaker_seat,
        text=text,
        co_declaration=co_declaration,
        addressed_seat_no=seat_no,
        addressed_seat_nos=seat_nos,
        role_callout=role_callout,
        created_at_ms=created_at_ms if created_at_ms is not None else now_ms(),
    )


def make_npc_generated_event(
    *,
    game_id: str,
    phase_id: str,
    day: int,
    phase: Phase,
    speaker_seat: int,
    text: str,
    co_declaration: str | None = None,
    addressed_seat_no: int | None = None,
    addressed_seat_nos: tuple[int, ...] | None = None,
    role_callout: str | None = None,
    claimed_seer_target_seat: int | None = None,
    claimed_seer_is_wolf: bool | None = None,
    claimed_medium_target_seat: int | None = None,
    claimed_medium_is_wolf: bool | None = None,
    created_at_ms: int | None = None,
) -> SpeechEvent:
    seat_no, seat_nos = _normalize_addressed(addressed_seat_no, addressed_seat_nos)
    return SpeechEvent(
        event_id=new_event_id(),
        game_id=game_id,
        phase_id=phase_id,
        day=day,
        phase=phase,
        source=SpeechSource.NPC_GENERATED,
        speaker_kind=SpeakerKind.NPC,
        speaker_seat=speaker_seat,
        text=text,
        co_declaration=co_declaration,
        addressed_seat_no=seat_no,
        addressed_seat_nos=seat_nos,
        role_callout=role_callout,
        claimed_seer_target_seat=claimed_seer_target_seat,
        claimed_seer_is_wolf=claimed_seer_is_wolf,
        claimed_medium_target_seat=claimed_medium_target_seat,
        claimed_medium_is_wolf=claimed_medium_is_wolf,
        created_at_ms=created_at_ms if created_at_ms is not None else now_ms(),
    )


def make_voice_stt_event(
    *,
    game_id: str,
    phase_id: str,
    day: int,
    phase: Phase,
    speaker_seat: int,
    text: str,
    stt_confidence: float,
    audio_start_ms: int,
    audio_end_ms: int,
    co_declaration: str | None = None,
    addressed_seat_no: int | None = None,
    addressed_seat_nos: tuple[int, ...] | None = None,
    created_at_ms: int | None = None,
) -> SpeechEvent:
    seat_no, seat_nos = _normalize_addressed(addressed_seat_no, addressed_seat_nos)
    return SpeechEvent(
        event_id=new_event_id(),
        game_id=game_id,
        phase_id=phase_id,
        day=day,
        phase=phase,
        source=SpeechSource.VOICE_STT,
        speaker_kind=SpeakerKind.HUMAN,
        speaker_seat=speaker_seat,
        text=text,
        stt_confidence=stt_confidence,
        audio_start_ms=audio_start_ms,
        audio_end_ms=audio_end_ms,
        co_declaration=co_declaration,
        addressed_seat_no=seat_no,
        addressed_seat_nos=seat_nos,
        created_at_ms=created_at_ms if created_at_ms is not None else now_ms(),
    )


# ---------------------------------------------------------------------- Service


class DiscussionService:
    """Front door for SpeechEvent ingestion + PLAYER_SPEECH side-effect dispatch.

    Every call to :meth:`record` performs the same three steps in order so the
    contract is uniform across all ingestion origins:

      1. Insert into `speech_events` (always).
      2. Append a `LogEntry(kind="PLAYER_SPEECH")` (skipped for `phase_baseline`).
      3. Post to the main text channel (skipped for `phase_baseline` and `text`).

    The PLAYER_SPEECH log preserves the existing public-log contract so post-game
    replay remains a single timeline. The skip rules are documented in the
    module docstring above.
    """

    PLAYER_SPEECH_KIND = "PLAYER_SPEECH"

    def __init__(
        self,
        store: SpeechEventStore,
        log_sink: PublicLogSink | None = None,
        message_poster: SpeechMessagePoster | None = None,
    ) -> None:
        self._store = store
        self._log_sink = log_sink
        self._poster = message_poster

    async def record_persist_only(self, event: SpeechEvent) -> None:
        """Persist the SpeechEvent without invoking PLAYER_SPEECH log + channel post.

        Use from rounds-mode backfill where the existing legacy path has already
        posted the message and inserted the LogEntry; this method captures the
        canonical event row for the speech-event-bus without duplication.
        """
        await self._store.insert(event)

    async def record(self, event: SpeechEvent) -> None:
        """Persist the event then run the post-write side-effects per source."""
        await self._store.insert(event)

        if event.source == SpeechSource.PHASE_BASELINE:
            return

        if self._log_sink is not None and event.speaker_seat is not None:
            log_entry = LogEntry(
                game_id=event.game_id,
                day=event.day,
                phase=event.phase,
                kind=self.PLAYER_SPEECH_KIND,
                actor_seat=event.speaker_seat,
                visibility="PUBLIC",
                text=event.text,
                created_at=event.created_at_ms // 1000,
            )
            try:
                await self._log_sink.insert_log_public(log_entry)
            except Exception:
                log.exception(
                    "PLAYER_SPEECH log insert failed",
                    extra={"event_id": event.event_id,
                           "source": event.source.value},
                )

        if self._poster is not None and event.source != SpeechSource.TEXT and event.text:
            try:
                await self._poster.post_public(event.game_id, event.text, self.PLAYER_SPEECH_KIND)
            except Exception:
                log.exception(
                    "PLAYER_SPEECH channel post failed",
                    extra={"event_id": event.event_id,
                           "source": event.source.value},
                )

    async def begin_phase(
        self,
        *,
        game_id: str,
        day: int,
        phase: Phase,
        alive_seat_nos: Iterable[int],
        sequence: int = 1,
    ) -> str:
        """Insert the `phase_baseline` sentinel and return the resulting `phase_id`.

        Call exactly once at the start of every public speech phase
        (`DAY_DISCUSSION`, `DAY_RUNOFF_SPEECH`). Subsequent `record()` calls in
        that phase MUST use the returned `phase_id`.
        """
        phase_id = make_phase_id(game_id, day, phase, sequence)
        sentinel = make_phase_baseline(
            game_id=game_id,
            phase_id=phase_id,
            day=day,
            phase=phase,
            alive_seat_nos=alive_seat_nos,
        )
        await self.record(sentinel)
        return phase_id

    async def begin_phase_if_absent(
        self,
        *,
        game_id: str,
        day: int,
        phase: Phase,
        alive_seat_nos: Iterable[int],
        sequence: int = 1,
    ) -> str:
        """Idempotent baseline insert: returns the canonical phase_id and only
        writes the sentinel if no events already exist for that phase_id.

        Use from background workers (LLM discussion / runoff) so a worker re-
        dispatch (recovery, force-skip, restart) does not duplicate the
        baseline; the canonical phase_id stays stable so downstream `record`s
        attach to the same phase fold.
        """
        phase_id = make_phase_id(game_id, day, phase, sequence)
        existing = await self._store.load_phase(game_id, phase_id)
        if existing:
            return phase_id
        sentinel = make_phase_baseline(
            game_id=game_id,
            phase_id=phase_id,
            day=day,
            phase=phase,
            alive_seat_nos=alive_seat_nos,
        )
        await self.record(sentinel)
        return phase_id

    async def load_phase(self, game_id: str, phase_id: str) -> Sequence[SpeechEvent]:
        return await self._store.load_phase(game_id, phase_id)

    async def load_for_game(self, game_id: str) -> Sequence[SpeechEvent]:
        """All non-baseline speech events for `game_id`, ordered by time.

        Exposed so the arbiter can extract historical CO claims across
        phase boundaries without going around the store interface.
        """
        return await self._store.load_for_game(game_id)


# ---------------------------------------------------------------------- Rebuild


def apply_speech_event(
    state: PublicDiscussionState | None,
    event: SpeechEvent,
) -> PublicDiscussionState | None:
    """Stepwise fold: produce the next `PublicDiscussionState` from a single event.

    The first event of a phase MUST be a `phase_baseline` sentinel; passing
    `state=None` with a non-sentinel event returns `None` (the caller is asking
    to fold an event into a phase whose baseline has not been seeded).

    A returned state is a *new* object; the input is not mutated. This makes
    the function safe to use as a `functools.reduce` step.
    """
    from wolfbot.domain.discussion import CoClaim

    if event.source == SpeechSource.PHASE_BASELINE:
        if event.alive_seat_nos_json is None:
            return None
        try:
            alive_list = json.loads(event.alive_seat_nos_json)
        except json.JSONDecodeError:
            return None
        return PublicDiscussionState(
            game_id=event.game_id,
            phase_id=event.phase_id,
            day=event.day,
            alive_seat_nos=frozenset(int(s) for s in alive_list),
        )

    if state is None:
        return None

    speaker = event.speaker_seat
    silent = set(state.silent_seats) if state.silent_seats else set(
        state.alive_seat_nos)
    if state.silent_seats == frozenset() and not state.recent_speech_event_ids:
        # First non-baseline event: seed silent_seats from alive baseline.
        silent = set(state.alive_seat_nos)
    if speaker is not None:
        silent.discard(speaker)

    co_claims = list(state.co_claims)
    seen_co = {(c.seat, c.role_claim) for c in co_claims}
    # Roles already CO'd by *some* seat before this event. Lets us
    # detect first-CO-of-role transitions for the counter-CO pool
    # trigger below without re-walking the full claim list.
    existing_role_keys = {role for (_, role) in seen_co}
    is_new_co = False  # tracks whether THIS event added a fresh CO
    fired_first_co_role: str | None = None
    if speaker is not None:
        role_key = _resolve_co_role(event)
        if role_key is not None:
            key = (speaker, role_key)
            if key not in seen_co:
                if role_key not in existing_role_keys:
                    fired_first_co_role = role_key
                seen_co.add(key)
                co_claims.append(
                    CoClaim(
                        seat=speaker,
                        role_claim=role_key,
                        declared_at_event_id=event.event_id,
                    )
                )
                is_new_co = True

    recent = [*state.recent_speech_event_ids, event.event_id][-10:]

    # Address routing rules. The arbiter consumes ``last_addressed_seats``
    # (multi-seat) to prioritize everyone who was just called out, so we
    # have to be careful about *when* members are cleared:
    #
    # - Human or NPC speech with its own ``addressed_seat_nos`` always
    #   supersedes the prior addressing (= a fresh call-out wins, the
    #   old set is dropped wholesale).
    # - An NPC who is themselves in the prior set speaks (= one of the
    #   addressees replied) → remove only that NPC from the set; the
    #   others stay prioritized so they all get a chance to answer.
    # - Anything else keeps the standing set. Without this, e.g. a
    #   silent_rotation pick that "jumps the line" before any addressed
    #   NPC replies would silently clear the hint and the addressees
    #   never get prioritized.
    last_addressed_seats: frozenset[int] = state.last_addressed_seats
    last_addressed_speaker_seat = state.last_addressed_speaker_seat
    last_addressed_text = state.last_addressed_text
    new_addressed = event_addressed_seats(event)
    if new_addressed:
        last_addressed_seats = frozenset(new_addressed)
        last_addressed_speaker_seat = speaker
        last_addressed_text = event.text
    elif (
        event.source == SpeechSource.NPC_GENERATED
        and speaker is not None
        and speaker in state.last_addressed_seats
    ):
        remaining = set(state.last_addressed_seats) - {speaker}
        last_addressed_seats = frozenset(remaining)
        if not remaining:
            last_addressed_speaker_seat = None
            last_addressed_text = ""
    last_addressed_seat = (
        next(iter(sorted(last_addressed_seats))) if last_addressed_seats else None
    )

    last_speaker_seat = (
        speaker if speaker is not None else state.last_speaker_seat
    )
    # Per-seat utterance count within the phase. Increment on every
    # non-baseline speaker; the arbiter's `_pick_key` reads this so an
    # NPC who's already spoken N times falls below NPCs who spoke
    # fewer times.
    speech_counts = dict(state.speech_counts)
    if speaker is not None:
        speech_counts[speaker] = speech_counts.get(speaker, 0) + 1
    # Append (speaker, has_info) to the sliding summary window the
    # arbiter uses for low-info pair-volley detection. ``has_info`` is
    # the structured "did this event move the discussion forward"
    # signal. Today: only a *first-time* CO counts (re-declaring an
    # already-recorded CO doesn't bypass the gate — that was the
    # ジョナス↔ラキオ ping-pong escape hatch where Raqio kept emitting
    # the same `co_declaration='seer'` flag and made every speech look
    # like new info). Wider signals (new accusation target, vote
    # announcement) can be added here later without changing the
    # field shape.
    summary = list(state.recent_speech_summary)
    if speaker is not None:
        summary.append((speaker, is_new_co))
        summary = summary[-6:]
    # Track outstanding role-callouts (e.g. "占い師の方どうぞ"). A request
    # adds the role to the pending set; a matching CO consumes it
    # (= the call was answered). Wolf-side NPCs and real role holders
    # both react to this in their speech prompt.
    pending_role_callouts = set(state.pending_role_callouts)
    if event.role_callout is not None:
        pending_role_callouts.add(event.role_callout)
    if is_new_co:
        # `_resolve_co_role` returned a role; remove it from the pending
        # set even if it wasn't explicitly requested — anyone CO'ing
        # implicitly answers any outstanding call.
        for role_key in tuple(pending_role_callouts):
            if event.co_declaration == role_key or _resolve_co_role(event) == role_key:
                pending_role_callouts.discard(role_key)
        # ``info_request`` is a generic info-seeking callout; once anyone
        # CO's any info role, the request is considered partially answered
        # and the priority pool steps down.
        pending_role_callouts.discard("info_request")

    # First-CO counter-response window. Adds the role to
    # ``pending_co_response`` exactly once per role per phase fold.
    # The arbiter's pool dispatch consumes members one-by-one via
    # ``_callout_pool_asked``; once everyone in the pool has been
    # asked, the empty effective-pool naturally falls through to
    # normal priority. The flag itself stays set for the remainder of
    # the phase — a 2nd CO of the same role does NOT clear it because
    # the per-user request explicitly asks for the pool to keep
    # rotating until every member has spoken or skipped.
    pending_co_response = set(state.pending_co_response)
    if fired_first_co_role is not None:
        pending_co_response.add(fired_first_co_role)
    return PublicDiscussionState(
        game_id=state.game_id,
        phase_id=state.phase_id,
        day=state.day,
        alive_seat_nos=state.alive_seat_nos,
        co_claims=tuple(co_claims),
        stances=state.stances,
        pressure=state.pressure,
        open_topics=state.open_topics,
        silent_seats=frozenset(silent),
        recent_speech_event_ids=tuple(recent),
        last_addressed_seat=last_addressed_seat,
        last_addressed_speaker_seat=last_addressed_speaker_seat,
        last_addressed_text=last_addressed_text,
        last_addressed_seats=last_addressed_seats,
        last_speaker_seat=last_speaker_seat,
        recent_speech_summary=tuple(summary),
        pending_role_callouts=frozenset(pending_role_callouts),
        pending_co_response=frozenset(pending_co_response),
        speech_counts=speech_counts,
    )


def rebuild_public_state_from_events(
    events: Sequence[SpeechEvent],
    *,
    prior_co_keys: frozenset[tuple[int, str]] = frozenset(),
) -> PublicDiscussionState | None:
    """Pure fold over a single phase's `SpeechEvent` rows.

    Returns ``None`` if `events` is empty or contains no sentinel — the caller
    decides whether to treat that as "phase not yet started" or as an error.

    MVP rules (per the spec delta + AC2):
      * `alive_seat_nos` is read from the sentinel's `alive_seat_nos_json`.
      * `co_claims` extracts seat-attributed CO mentions in declaration order.
      * `silent_seats` = `alive_seat_nos` minus seats with ≥1 non-sentinel event.
      * `recent_speech_event_ids` keeps the last 10 non-sentinel ids in arrival order.
      * `stances` / `pressure` / `open_topics` remain empty in MVP — design defers.

    ``prior_co_keys`` seeds the internal ``seen_co`` set with `(seat, role)`
    tuples extracted from earlier-phase events. Without this, a seat that
    CO'd on day 1 and re-asserts the same CO on day 2 would flag
    ``is_new_co=True`` in the day-2 phase rebuild (because the per-phase
    fold starts ``seen_co`` empty). That defeats the volley-demotion gate
    in ``speak_arbiter._compute_demoted_seats``: the ジョナス↔ユリコ
    ping-pong observed in game ``a701a7531dca`` day 2 escaped demotion
    because every ジョナス re-CO looked like fresh info to the per-phase
    fold even though the day-1 seer CO had been on record.
    """
    if not events:
        return None

    sentinel = next(
        (e for e in events if e.source == SpeechSource.PHASE_BASELINE),
        None,
    )
    if sentinel is None or sentinel.alive_seat_nos_json is None:
        return None

    try:
        alive_list = json.loads(sentinel.alive_seat_nos_json)
    except json.JSONDecodeError:
        return None
    alive_seats: frozenset[int] = frozenset(int(s) for s in alive_list)

    state = PublicDiscussionState(
        game_id=sentinel.game_id,
        phase_id=sentinel.phase_id,
        day=sentinel.day,
        alive_seat_nos=alive_seats,
    )

    from wolfbot.domain.discussion import CoClaim

    spoken_seats: set[int] = set()
    co_claims: list[CoClaim] = []
    recent_ids: list[str] = []
    summary: list[tuple[int, bool]] = []
    # Seed seen_co with prior-phase CO history so a re-asserted CO doesn't
    # flag is_new_co=True. co_claims itself stays scoped to current-phase
    # declarations so the per-phase fold's outward shape is unchanged —
    # the arbiter overrides state.co_claims with the game-wide history
    # right after the rebuild via extract_co_claims_from_events.
    seen_co: set[tuple[int, str]] = set(prior_co_keys)
    pending_role_callouts: set[str] = set()
    pending_co_response: set[str] = set()
    speech_counts: dict[int, int] = {}
    last_addressed_seats: frozenset[int] = frozenset()
    last_addressed_speaker_seat: int | None = None
    last_addressed_text: str = ""
    last_speaker_seat: int | None = None
    for event in events:
        if event.source == SpeechSource.PHASE_BASELINE:
            continue
        if event.speaker_seat is not None:
            spoken_seats.add(event.speaker_seat)
            last_speaker_seat = event.speaker_seat
            speech_counts[event.speaker_seat] = (
                speech_counts.get(event.speaker_seat, 0) + 1
            )
        recent_ids.append(event.event_id)
        # Mirror the per-event update logic in `apply_speech_event`:
        # a fresh ``addressed_seat_nos`` (multi-seat) replaces the entire
        # standing set; an NPC speaker who's already in the set consumes
        # *only their own slot*, leaving co-addressees still prioritized.
        new_addressed = event_addressed_seats(event)
        if new_addressed:
            last_addressed_seats = frozenset(new_addressed)
            last_addressed_speaker_seat = event.speaker_seat
            last_addressed_text = event.text
        elif (
            event.source == SpeechSource.NPC_GENERATED
            and event.speaker_seat is not None
            and event.speaker_seat in last_addressed_seats
        ):
            remaining = set(last_addressed_seats) - {event.speaker_seat}
            last_addressed_seats = frozenset(remaining)
            if not remaining:
                last_addressed_speaker_seat = None
                last_addressed_text = ""
        if event.speaker_seat is None:
            continue
        # Track outstanding role-callouts (request → pending; matching
        # CO consumes from the set). Per-event update mirror.
        if event.role_callout is not None:
            pending_role_callouts.add(event.role_callout)
        # `is_new_co` flag goes into `recent_speech_summary` so the arbiter
        # can detect "two seats arguing without new information" — a
        # repeated CO from the same seat does NOT count as info.
        is_new_co = False
        role_key = _resolve_co_role(event)
        if role_key is not None:
            key = (event.speaker_seat, role_key)
            if key not in seen_co:
                is_new_co = True
        summary.append((event.speaker_seat, is_new_co))
        if role_key is None:
            continue
        if is_new_co:
            pending_role_callouts.discard(role_key)
            # See integrate_speech_event: info_request is consumed by any
            # info-role CO, regardless of which specific role was asked.
            pending_role_callouts.discard("info_request")
            # First-CO of role triggers the counter-CO opportunity pool.
            # Mirror of ``apply_speech_event`` — fired once per role per
            # phase fold (subsequent COs of the same role keep the
            # window open until the arbiter exhausts the asked tracker).
            existing_role_keys = {role for (_, role) in seen_co}
            if role_key not in existing_role_keys:
                pending_co_response.add(role_key)
        if (event.speaker_seat, role_key) in seen_co:
            continue
        seen_co.add((event.speaker_seat, role_key))
        co_claims.append(
            CoClaim(
                seat=event.speaker_seat,
                role_claim=role_key,
                declared_at_event_id=event.event_id,
            )
        )

    state.co_claims = tuple(co_claims)
    state.silent_seats = frozenset(alive_seats - spoken_seats)
    state.recent_speech_event_ids = tuple(recent_ids[-10:])
    state.last_addressed_seats = last_addressed_seats
    state.last_addressed_seat = (
        next(iter(sorted(last_addressed_seats))) if last_addressed_seats else None
    )
    state.last_addressed_speaker_seat = last_addressed_speaker_seat
    state.last_addressed_text = last_addressed_text
    state.last_speaker_seat = last_speaker_seat
    state.recent_speech_summary = tuple(summary[-6:])
    state.pending_role_callouts = frozenset(pending_role_callouts)
    state.pending_co_response = frozenset(pending_co_response)
    state.speech_counts = speech_counts
    return state


_VALID_CO_ROLES: frozenset[str] = frozenset(CO_CLAIM_VALUES)

_CO_MARKERS: tuple[tuple[str, str], ...] = (
    ("seer", "占いCO"),
    ("medium", "霊媒CO"),
    ("knight", "騎士CO"),
)
"""Legacy substring fallback. Authoritative CO comes from
`SpeechEvent.co_declaration` (set at the source: NPC/LLM schema field, or
Gemini's structured `co_claim`). Substring matching is only used for legacy
events and human text where natural-language CO has not been pre-tagged.
"""

# Role keyword + canonical CO token per role. Used by
# :func:`_text_contains_self_declaration` to verify the structured
# ``co_declaration`` field matches an actual self-naming in ``text``.
_ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "seer": ("占い師",),
    "medium": ("霊媒師",),
    "knight": ("騎士",),
}
_ROLE_CO_TOKENS: dict[str, str] = {
    "seer": "占いCO",
    "medium": "霊媒CO",
    "knight": "騎士CO",
}
# Japanese first-person pronouns used by NPCs / human players. Order
# matters: longer pronouns (``この身``, ``わたくし``) first so the loop
# strips them before partial overlaps catch shorter forms.
_FIRST_PERSON_PRONOUNS: tuple[str, ...] = (
    "わたくし",
    "アタシ",
    "わたし",
    "オレ",
    "この身",
    "拙者",
    "アタイ",
    "ボク",
    "私",
    "僕",
    "俺",
    "我",
    "わて",
)
# Phrases that, when they appear immediately before a role keyword, mean
# the role is being mentioned topically (= about someone else / about
# the concept) rather than self-declared. Stripping them out of the
# text before looking for the self-decl pattern prevents the
# "対抗占い師は出ないのか?" false positive that incorrectly tagged
# game ``98e5a083b5ff`` ラキオ as a seer-CO before he actually CO'd.
_TOPICAL_PREFIXES: tuple[str, ...] = (
    "対抗の", "対抗", "別の", "他の", "もう一人の", "もうひとりの",
    "誰か", "他に",
)
# Verb endings that, when they directly follow a role keyword (or the
# canonical ``XCO`` token), turn a bare keyword into a self-declaration
# even without an explicit first-person pronoun. Covers the persona
# voice patterns the existing personas use ("〜だよ" / "〜なのです" /
# "〜なんだ" / "〜やってます" / "〜になります").
_DECL_VERB_PATTERN = (
    r"(?:です|ですわ|なんだ|だよ|だぜ|なの|なる|なります|"
    r"になります|やってる|やってます|として(?:出る|出ます|名乗る|名乗ります)|"
    r"に出ます|に出る|だ|ね|よ|わ)"
)


def _text_contains_self_declaration(text: str, role: str) -> bool:
    """Heuristic: does ``text`` contain a first-person CO declaration of ``role``?

    Used as a sanity check on the structured ``co_declaration`` field.
    The NPC LLM occasionally leaks its strategic intent into the
    structured field before the natural-language text actually contains
    the declaration (game ``98e5a083b5ff`` day 1: ラキオ the madman
    posted a question to Stella with ``co_declaration='seer'`` set
    even though the text was just "対抗占い師は出ないのか?"). When the
    text/structured pair disagree, this helper returns ``False`` so
    :func:`_resolve_co_role` falls back to the legacy substring scan
    rather than blindly trusting the leaked flag.

    Recognised patterns (any one suffices):

    * **First-person pronoun within ~10 characters of the role keyword.**
      Topical / counter-CO-request prefixes ("対抗", "別の", "他の", ...)
      are stripped first so "対抗占い師" can't be mistaken for self-decl.
    * **Role keyword (or canonical ``XCO`` token) immediately followed
      by a declarative verb ending** ("占い師です" / "霊媒CO する" /
      "騎士やってます" / ...). The verb whitelist is curated to match
      the personas' speech profiles without bleeding into question /
      negation forms ("出ないのか" / "ではない" / "誰なんだろう").
    """
    keywords = _ROLE_KEYWORDS.get(role)
    co_token = _ROLE_CO_TOKENS.get(role)
    if not keywords or co_token is None:
        return False

    # Strip topical / counter-CO-request prefixes so "対抗占い師" doesn't
    # produce a false positive in either pattern below.
    cleaned = text
    for prefix in _TOPICAL_PREFIXES:
        for kw in keywords:
            cleaned = cleaned.replace(prefix + kw, "")
        cleaned = cleaned.replace(prefix + co_token, "")

    pronoun_alt = "|".join(re.escape(p) for p in _FIRST_PERSON_PRONOUNS)
    for kw in keywords:
        # Pattern A: first-person pronoun, optional particle/comma, then
        # the role keyword within ~10 chars (skipping whitespace and
        # punctuation that don't end a sentence — `。` / newlines do).
        pat_a = re.compile(
            rf"(?:{pronoun_alt})"
            rf"(?:[、,はがこそも自身]{{0,3}})?"
            rf"[^。\n]{{0,10}}"
            rf"{re.escape(kw)}"
        )
        if pat_a.search(cleaned):
            return True
        # Pattern B: keyword + declarative verb suffix (no first-person
        # required — the verb form itself signals self-naming).
        pat_b = re.compile(
            rf"{re.escape(kw)}[、,]?{_DECL_VERB_PATTERN}"
        )
        if pat_b.search(cleaned):
            return True

    # Pattern C: canonical ``XCO`` token followed by a declarative verb
    # or used as a bare self-marker. ``占いCOがいない`` / ``占いCOについて``
    # are topical and rejected by the trailing-verb requirement.
    pat_c = re.compile(
        rf"{re.escape(co_token)}(?:します|する|です|なんだ|だよ|だ|に出ます|に出る)"
    )
    if pat_c.search(cleaned):
        return True
    # Pattern D: first-person pronoun followed by ``XCO`` within ~10 chars,
    # covering "私こそ占いCO" / "僕、霊媒CO" etc.
    pat_d = re.compile(
        rf"(?:{pronoun_alt})"
        rf"(?:[、,はがこそも自身]{{0,3}})?"
        rf"[^。\n]{{0,10}}"
        rf"{re.escape(co_token)}"
    )
    return bool(pat_d.search(cleaned))


def _resolve_co_role(event: SpeechEvent) -> str | None:
    """Pick the CO role for an event, preferring the structured field.

    Returns one of ``"seer" / "medium" / "knight"`` or ``None`` for "no CO".

    The structured ``co_declaration`` field is treated as authoritative
    *only when ``text`` carries a matching self-declaration*. Without
    the text guard, the NPC LLM could (and did, in game
    ``98e5a083b5ff``) leak its strategic intent into the structured
    field while the text was still a question / topical mention, which
    falsely flipped the speaker's seat into the public CO ledger one
    turn early. When the structured field disagrees with the text, we
    drop the flag and fall through to the legacy substring scan so
    only the explicit canonical ``占いCO``/``霊媒CO``/``騎士CO`` token
    in ``text`` survives.
    """
    declared = event.co_declaration
    if declared is not None and declared in _VALID_CO_ROLES:
        if _text_contains_self_declaration(event.text, declared):
            return declared
        log.info(
            "co_declaration_text_mismatch dropped event=%s declared=%s text=%r",
            event.event_id, declared, event.text[:120],
        )
    for role_key, marker in _CO_MARKERS:
        if marker in event.text:
            return role_key
    return None


def extract_co_claims_from_events(
    events: Sequence[SpeechEvent],
) -> tuple[CoClaim, ...]:
    """Walk events of a single game and return the de-duplicated CO claims.

    Used by SpeakArbiter to carry CO claims across phase boundaries so the
    NPC's prompt still shows "席4 seerCO" on day 2 even though the day-2
    PublicDiscussionState fold only sees day-2 events. De-dup key is
    ``(speaker_seat, role_claim)`` — the earliest event wins, matching
    the in-phase fold semantics.
    """
    claims: list[CoClaim] = []
    seen: set[tuple[int, str]] = set()
    for event in events:
        if event.source == SpeechSource.PHASE_BASELINE:
            continue
        if event.speaker_seat is None:
            continue
        role_key = _resolve_co_role(event)
        if role_key is None:
            continue
        key = (event.speaker_seat, role_key)
        if key in seen:
            continue
        seen.add(key)
        claims.append(
            CoClaim(
                seat=event.speaker_seat,
                role_claim=role_key,
                declared_at_event_id=event.event_id,
            )
        )
    return tuple(claims)
