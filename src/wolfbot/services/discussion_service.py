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
import time
import uuid
from collections.abc import Iterable, Sequence
from typing import Any, Protocol, runtime_checkable

import aiosqlite

from wolfbot.domain.discussion import (
    PublicDiscussionState,
    SpeakerKind,
    SpeechEvent,
    SpeechSource,
    make_phase_id,
)
from wolfbot.domain.enums import Phase
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
        await self._conn.execute(
            """
            INSERT INTO speech_events (
                event_id, game_id, phase_id, day, phase, source, speaker_kind,
                speaker_seat, text, stt_confidence, audio_start_ms, audio_end_ms,
                alive_seat_nos_json, summary, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                event.created_at_ms,
            ),
        )
        await self._conn.commit()

    async def load_phase(self, game_id: str, phase_id: str) -> Sequence[SpeechEvent]:
        async with self._conn.execute(
            """
            SELECT event_id, game_id, phase_id, day, phase, source, speaker_kind,
                   speaker_seat, text, stt_confidence, audio_start_ms, audio_end_ms,
                   alive_seat_nos_json, summary, created_at_ms
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
                   alive_seat_nos_json, summary, created_at_ms
              FROM speech_events
             WHERE game_id=?
             ORDER BY created_at_ms ASC, event_id ASC
            """,
            (game_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_event(row) for row in rows]


def _row_to_event(row: Any) -> SpeechEvent:
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
        created_at_ms=row[14],
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


def make_human_text_event(
    *,
    game_id: str,
    phase_id: str,
    day: int,
    phase: Phase,
    speaker_seat: int,
    text: str,
    created_at_ms: int | None = None,
) -> SpeechEvent:
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
    created_at_ms: int | None = None,
) -> SpeechEvent:
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
    created_at_ms: int | None = None,
) -> SpeechEvent:
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
    if speaker is not None:
        for role_key, marker in _CO_MARKERS:
            if marker in event.text:
                key = (speaker, role_key)
                if key not in seen_co:
                    seen_co.add(key)
                    co_claims.append(
                        CoClaim(
                            seat=speaker,
                            role_claim=role_key,
                            declared_at_event_id=event.event_id,
                        )
                    )
                break

    recent = [*state.recent_speech_event_ids, event.event_id][-10:]

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
    )


def rebuild_public_state_from_events(
    events: Sequence[SpeechEvent],
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
    seen_co: set[tuple[int, str]] = set()
    for event in events:
        if event.source == SpeechSource.PHASE_BASELINE:
            continue
        if event.speaker_seat is not None:
            spoken_seats.add(event.speaker_seat)
        recent_ids.append(event.event_id)
        for role_key, marker in _CO_MARKERS:
            if marker in event.text and event.speaker_seat is not None:
                key = (event.speaker_seat, role_key)
                if key in seen_co:
                    continue
                seen_co.add(key)
                co_claims.append(
                    CoClaim(
                        seat=event.speaker_seat,
                        role_claim=role_key,
                        declared_at_event_id=event.event_id,
                    )
                )
                break

    state.co_claims = tuple(co_claims)
    state.silent_seats = frozenset(alive_seats - spoken_seats)
    state.recent_speech_event_ids = tuple(recent_ids[-10:])
    return state


_CO_MARKERS: tuple[tuple[str, str], ...] = (
    ("seer", "占いCO"),
    ("medium", "霊媒CO"),
    ("knight", "騎士CO"),
)
