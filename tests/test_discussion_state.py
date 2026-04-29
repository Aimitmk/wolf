"""Bundle 1 tests — SpeechEvent persistence, sentinel insertion, post-write hooks,
and PublicDiscussionState rebuild.

Covers tasks 1.4 and 1.5 of the speech-event-bus-foundation bundle:
  * SpeechEvent round-trip through SqliteSpeechEventStore.
  * begin_phase() inserts a sentinel and returns the canonical phase_id.
  * DiscussionService.record() emits PLAYER_SPEECH LogEntry per source.
  * DiscussionService.record() skips channel post for source=text.
  * DiscussionService.record() skips both LogEntry and channel post for sentinels.
  * rebuild_public_state_from_events() is bitwise reproducible from a fresh fold.
  * silent_seats correctly excludes dead seats (via the sentinel baseline).
  * co_claims are detected from canonical CO tokens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from wolfbot.domain.discussion import SpeakerKind, SpeechSource, make_phase_id
from wolfbot.domain.enums import Phase
from wolfbot.domain.models import LogEntry
from wolfbot.persistence.schema import migrate
from wolfbot.services.discussion_service import (
    DiscussionService,
    SqliteSpeechEventStore,
    make_human_text_event,
    make_npc_generated_event,
    make_voice_stt_event,
    rebuild_public_state_from_events,
)


@pytest_asyncio.fixture
async def store_conn(tmp_path: Path) -> AsyncIterator:
    import aiosqlite

    db_path = tmp_path / "speech.db"
    await migrate(db_path)
    conn = await aiosqlite.connect(str(db_path))
    try:
        yield conn
    finally:
        await conn.close()


# ----- in-memory recording fakes for the service hooks ------------------------------


class _RecordingLogSink:
    def __init__(self) -> None:
        self.entries: list[LogEntry] = []

    async def insert_log_public(self, entry: LogEntry) -> None:
        self.entries.append(entry)


class _RecordingPoster:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []

    async def post_public(self, game_id: str, text: str, kind: str) -> None:
        self.posts.append((game_id, text, kind))


# ----- store round-trip --------------------------------------------------------------


async def test_speech_event_round_trip_persists_all_fields(store_conn) -> None:
    store = SqliteSpeechEventStore(store_conn)
    phase_id = make_phase_id("g1", 1, Phase.DAY_DISCUSSION)
    voice_event = make_voice_stt_event(
        game_id="g1",
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=3,
        text="P1のCOちょっと遅くなかった？",
        stt_confidence=0.91,
        audio_start_ms=72100,
        audio_end_ms=74400,
        created_at_ms=1_710_000_000_000,
    )

    await store.insert(voice_event)
    rows = await store.load_phase("g1", phase_id)

    assert len(rows) == 1
    loaded = rows[0]
    assert loaded == voice_event


async def test_load_phase_filters_by_phase_id(store_conn) -> None:
    store = SqliteSpeechEventStore(store_conn)
    pid_a = make_phase_id("g1", 1, Phase.DAY_DISCUSSION, sequence=1)
    pid_b = make_phase_id("g1", 1, Phase.DAY_DISCUSSION, sequence=2)

    e1 = make_human_text_event(
        game_id="g1",
        phase_id=pid_a,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=1,
        text="まず情報整理しよう",
        created_at_ms=1,
    )
    e2 = make_npc_generated_event(
        game_id="g1",
        phase_id=pid_b,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=5,
        text="占いCOがいないのは不自然",
        created_at_ms=2,
    )
    await store.insert(e1)
    await store.insert(e2)

    rows_a = await store.load_phase("g1", pid_a)
    rows_b = await store.load_phase("g1", pid_b)

    assert [r.event_id for r in rows_a] == [e1.event_id]
    assert [r.event_id for r in rows_b] == [e2.event_id]


# ----- begin_phase + sentinel --------------------------------------------------------


async def test_begin_phase_inserts_sentinel_and_returns_phase_id(store_conn) -> None:
    store = SqliteSpeechEventStore(store_conn)
    log_sink = _RecordingLogSink()
    poster = _RecordingPoster()
    service = DiscussionService(store, log_sink=log_sink, message_poster=poster)

    phase_id = await service.begin_phase(
        game_id="g1",
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=[1, 2, 3, 4, 5, 6, 7, 8, 9],
    )

    assert phase_id == make_phase_id("g1", 1, Phase.DAY_DISCUSSION)
    rows = await store.load_phase("g1", phase_id)
    assert len(rows) == 1
    sentinel = rows[0]
    assert sentinel.source == SpeechSource.PHASE_BASELINE
    assert sentinel.speaker_kind == SpeakerKind.SYSTEM
    assert sentinel.speaker_seat is None
    # sentinels emit no PLAYER_SPEECH and no main-channel post
    assert log_sink.entries == []
    assert poster.posts == []


async def test_sentinel_alive_seat_nos_are_sorted_unique(store_conn) -> None:
    store = SqliteSpeechEventStore(store_conn)
    service = DiscussionService(store)
    phase_id = await service.begin_phase(
        game_id="g1",
        day=2,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=[3, 1, 1, 5, 2],  # duplicated and unsorted
    )
    rows = await service.load_phase("g1", phase_id)
    assert rows[0].alive_seat_nos_json == "[1, 2, 3, 5]"


# ----- record() side-effect dispatch ------------------------------------------------


async def test_record_text_skips_channel_post_emits_log(store_conn) -> None:
    store = SqliteSpeechEventStore(store_conn)
    log_sink = _RecordingLogSink()
    poster = _RecordingPoster()
    service = DiscussionService(store, log_sink=log_sink, message_poster=poster)

    phase_id = await service.begin_phase(
        game_id="g1",
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=range(1, 10),
    )
    text_event = make_human_text_event(
        game_id="g1",
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=2,
        text="占いCOまだですか？",
    )

    await service.record(text_event)

    assert len(log_sink.entries) == 1
    entry = log_sink.entries[0]
    assert entry.kind == DiscussionService.PLAYER_SPEECH_KIND
    assert entry.actor_seat == 2
    assert entry.text == "占いCOまだですか？"
    assert entry.visibility == "PUBLIC"
    # the original Discord message is the channel post — service must not duplicate
    assert poster.posts == []


async def test_record_voice_stt_emits_log_and_channel_post(store_conn) -> None:
    store = SqliteSpeechEventStore(store_conn)
    log_sink = _RecordingLogSink()
    poster = _RecordingPoster()
    service = DiscussionService(store, log_sink=log_sink, message_poster=poster)

    phase_id = await service.begin_phase(
        game_id="g1",
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=range(1, 10),
    )
    voice_event = make_voice_stt_event(
        game_id="g1",
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=4,
        text="P1のCOタイミングは怪しい",
        stt_confidence=0.82,
        audio_start_ms=10,
        audio_end_ms=2010,
    )

    await service.record(voice_event)

    assert len(log_sink.entries) == 1
    assert log_sink.entries[0].kind == DiscussionService.PLAYER_SPEECH_KIND
    assert poster.posts == [
        ("g1", "P1のCOタイミングは怪しい", DiscussionService.PLAYER_SPEECH_KIND)
    ]


async def test_record_npc_generated_emits_log_and_channel_post(store_conn) -> None:
    store = SqliteSpeechEventStore(store_conn)
    log_sink = _RecordingLogSink()
    poster = _RecordingPoster()
    service = DiscussionService(store, log_sink=log_sink, message_poster=poster)

    phase_id = await service.begin_phase(
        game_id="g1",
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=range(1, 10),
    )
    npc_event = make_npc_generated_event(
        game_id="g1",
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=5,
        text="今は決め打たない方がよさそう",
    )

    await service.record(npc_event)

    assert len(log_sink.entries) == 1
    assert log_sink.entries[0].actor_seat == 5
    assert poster.posts == [
        ("g1", "今は決め打たない方がよさそう", DiscussionService.PLAYER_SPEECH_KIND)
    ]


# ----- rebuild ----------------------------------------------------------------------


async def test_rebuild_returns_none_for_phase_without_sentinel(store_conn) -> None:
    store = SqliteSpeechEventStore(store_conn)
    pid = make_phase_id("g1", 1, Phase.DAY_DISCUSSION)
    e = make_human_text_event(
        game_id="g1",
        phase_id=pid,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=1,
        text="hi",
        created_at_ms=1,
    )
    await store.insert(e)
    rows = await store.load_phase("g1", pid)
    assert rebuild_public_state_from_events(rows) is None


async def test_rebuild_silent_seats_excludes_dead_seats(store_conn) -> None:
    store = SqliteSpeechEventStore(store_conn)
    service = DiscussionService(store)
    # day 2: seats 1, 3, 4, 6, 7, 8, 9 alive; seats 2 and 5 already dead
    alive = [1, 3, 4, 6, 7, 8, 9]
    phase_id = await service.begin_phase(
        game_id="g1",
        day=2,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=alive,
    )
    # seats 1, 3, 4 speak; 6, 7, 8, 9 stay silent
    for i, seat in enumerate([1, 3, 4]):
        await service.record(
            make_human_text_event(
                game_id="g1",
                phase_id=phase_id,
                day=2,
                phase=Phase.DAY_DISCUSSION,
                speaker_seat=seat,
                text=f"発言{seat}",
                created_at_ms=100 + i,
            )
        )

    rows = await service.load_phase("g1", phase_id)
    state = rebuild_public_state_from_events(rows)
    assert state is not None
    assert state.alive_seat_nos == frozenset(alive)
    # silent_seats includes only alive non-speakers — dead seats 2 and 5 must NOT appear
    assert state.silent_seats == frozenset({6, 7, 8, 9})
    assert 2 not in state.silent_seats
    assert 5 not in state.silent_seats


async def test_rebuild_co_claims_capture_canonical_tokens(store_conn) -> None:
    store = SqliteSpeechEventStore(store_conn)
    service = DiscussionService(store)
    phase_id = await service.begin_phase(
        game_id="g1",
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=range(1, 10),
    )
    await service.record(
        make_human_text_event(
            game_id="g1",
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=4,
            text="占いCOします。1白",
            created_at_ms=10,
        )
    )
    await service.record(
        make_npc_generated_event(
            game_id="g1",
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=4,
            text="占いCO（再宣言）",
            created_at_ms=11,
        )
    )
    await service.record(
        make_human_text_event(
            game_id="g1",
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=7,
            text="霊媒COです",
            created_at_ms=12,
        )
    )

    state = rebuild_public_state_from_events(await service.load_phase("g1", phase_id))
    assert state is not None
    role_claims = sorted((c.seat, c.role_claim) for c in state.co_claims)
    assert role_claims == [(4, "seer"), (7, "medium")]


async def test_rebuild_is_deterministic_from_fresh_fold(store_conn) -> None:
    store = SqliteSpeechEventStore(store_conn)
    service = DiscussionService(store)
    phase_id = await service.begin_phase(
        game_id="g1",
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=range(1, 10),
    )
    for i, (seat, text) in enumerate(
        [(1, "占いCO"), (2, "もう少し聞かせて"), (3, "霊媒COお願いします")], start=10
    ):
        await service.record(
            make_human_text_event(
                game_id="g1",
                phase_id=phase_id,
                day=1,
                phase=Phase.DAY_DISCUSSION,
                speaker_seat=seat,
                text=text,
                created_at_ms=i,
            )
        )

    rows = await service.load_phase("g1", phase_id)
    s1 = rebuild_public_state_from_events(rows)
    s2 = rebuild_public_state_from_events(rows)
    # Pure fold: two rebuilds from the same input must be equal in all observable fields.
    assert s1 is not None and s2 is not None
    assert s1.alive_seat_nos == s2.alive_seat_nos
    assert s1.co_claims == s2.co_claims
    assert s1.silent_seats == s2.silent_seats
    assert s1.recent_speech_event_ids == s2.recent_speech_event_ids


# ----- failure-tolerance: log/poster errors must not break the write ----------------


class _FailingPoster:
    async def post_public(self, *_args, **_kwargs) -> None:
        raise RuntimeError("discord down")


class _FailingLogSink:
    async def insert_log_public(self, _entry: LogEntry) -> None:
        raise RuntimeError("logs_public down")


async def test_record_swallows_post_failure_so_persistence_still_succeeds(store_conn) -> None:
    store = SqliteSpeechEventStore(store_conn)
    service = DiscussionService(
        store,
        log_sink=_FailingLogSink(),
        message_poster=_FailingPoster(),
    )
    phase_id = await service.begin_phase(
        game_id="g1",
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=range(1, 10),
    )
    npc_event = make_npc_generated_event(
        game_id="g1",
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=5,
        text="hi",
    )
    await service.record(npc_event)
    rows = await service.load_phase("g1", phase_id)
    # sentinel + 1 npc event persisted even though the post-write hooks raised
    assert len(rows) == 2
