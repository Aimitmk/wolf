"""Round-trip tests for the claim columns on ``speech_events``.

The claim aggregator (`wolfbot.master.claim.claim_history`) is unit-tested
in :mod:`tests.test_claim_history`; this file pins the persistence
seam so a column rename or a missed migration block fails noisily.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from wolfbot.domain.discussion import (
    make_phase_id,
)
from wolfbot.domain.enums import Phase
from wolfbot.domain.ws_messages import (
    ClaimedSeerResult,
    LogicPacket,
    SpeakRequest,
)
from wolfbot.npc.openai_compatible_generator import _parse_claim_fields
from wolfbot.npc.speech_service import (
    NpcGeneratedSpeech,
    NpcSpeechService,
)
from wolfbot.persistence.schema import migrate
from wolfbot.services.discussion_service import (
    SqliteSpeechEventStore,
    make_npc_generated_event,
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


async def test_speech_event_round_trip_persists_seer_claim(store_conn) -> None:
    """A SpeechEvent carrying ``claimed_seer_target_seat`` /
    ``claimed_seer_is_wolf`` reads back identically. SQLite's lack of
    native bool means the integer ↔ bool coercion has to survive the
    round-trip — a regression here would silently drop every claim
    on Master restart."""
    store = SqliteSpeechEventStore(store_conn)
    phase_id = make_phase_id("g1", 1, Phase.DAY_DISCUSSION)
    event = make_npc_generated_event(
        game_id="g1",
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=2,
        text="昨夜セツを占って白でした。",
        co_declaration="seer",
        claimed_seer_target_seat=6,
        claimed_seer_is_wolf=False,
        created_at_ms=1_700_000_000_000,
    )

    await store.insert(event)
    rows = await store.load_phase("g1", phase_id)

    assert len(rows) == 1
    loaded = rows[0]
    assert loaded.claimed_seer_target_seat == 6
    assert loaded.claimed_seer_is_wolf is False
    assert loaded.claimed_medium_target_seat is None
    assert loaded.claimed_medium_is_wolf is None


async def test_speech_event_round_trip_persists_medium_void(store_conn) -> None:
    """``claimed_medium_is_wolf=None`` (= 'no execution yesterday')
    must round-trip through the 0/1/NULL column without being
    coerced into ``False``."""
    store = SqliteSpeechEventStore(store_conn)
    phase_id = make_phase_id("g2", 2, Phase.DAY_DISCUSSION)
    event = make_npc_generated_event(
        game_id="g2",
        phase_id=phase_id,
        day=2,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=5,
        text="昨日は処刑がなかったので結果は出ません。",
        co_declaration="medium",
        claimed_medium_target_seat=3,
        claimed_medium_is_wolf=None,
        created_at_ms=1_700_000_001_000,
    )

    await store.insert(event)
    rows = await store.load_phase("g2", phase_id)

    assert rows[0].claimed_medium_target_seat == 3
    assert rows[0].claimed_medium_is_wolf is None


async def test_legacy_speech_event_with_no_claim_loads_as_none(store_conn) -> None:
    """Pre-claim-column rows have NULL in every claim column. Loading
    an event without claims must not produce phantom entries."""
    store = SqliteSpeechEventStore(store_conn)
    phase_id = make_phase_id("g3", 1, Phase.DAY_DISCUSSION)
    event = make_npc_generated_event(
        game_id="g3",
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=4,
        text="（普通の議論）",
        created_at_ms=1_700_000_002_000,
    )
    await store.insert(event)

    rows = await store.load_phase("g3", phase_id)

    loaded = rows[0]
    assert loaded.claimed_seer_target_seat is None
    assert loaded.claimed_seer_is_wolf is None
    assert loaded.claimed_medium_target_seat is None
    assert loaded.claimed_medium_is_wolf is None


# ----------------------------------------------- LLM-output parsing


def test_parse_claim_fields_accepts_valid_seer_claim() -> None:
    seat, verdict = _parse_claim_fields(
        {"target_seat": 5, "is_wolf": True}, allow_null_verdict=False,
    )
    assert seat == 5
    assert verdict is True


def test_parse_claim_fields_drops_seer_claim_with_null_verdict() -> None:
    """Seer verdicts must be a concrete bool. ``null`` is medium-only
    semantics and would fabricate a result if accepted."""
    seat, verdict = _parse_claim_fields(
        {"target_seat": 5, "is_wolf": None}, allow_null_verdict=False,
    )
    assert seat is None
    assert verdict is None


def test_parse_claim_fields_accepts_medium_void() -> None:
    seat, verdict = _parse_claim_fields(
        {"target_seat": 3, "is_wolf": None}, allow_null_verdict=True,
    )
    assert seat == 3
    assert verdict is None


def test_parse_claim_fields_rejects_out_of_range_target() -> None:
    seat, verdict = _parse_claim_fields(
        {"target_seat": 99, "is_wolf": False}, allow_null_verdict=False,
    )
    assert seat is None
    assert verdict is None


# ----------------------------------------------- speech-service handoff


async def test_speech_service_threads_claim_into_speak_result() -> None:
    """``NpcSpeechService.respond`` must lift the generated speech's
    ``claimed_*`` fields onto the wire model so Master persists them
    on the SpeechEvent."""
    from wolfbot.npc.speech_service import FakeNpcGenerator

    speech = NpcGeneratedSpeech(
        text="昨夜セツを占って白だった。",
        intent="speak",
        used_logic_ids=(),
        estimated_duration_ms=2000,
        co_declaration="seer",
        claimed_seer_target_seat=6,
        claimed_seer_is_wolf=False,
    )
    gen = FakeNpcGenerator(default=speech)
    service = NpcSpeechService(gen)

    request = SpeakRequest(
        ts=1,
        trace_id="t",
        request_id="rq",
        phase_id="g::day1::DAY_DISCUSSION::1",
        npc_id="npc",
        seat_no=2,
        logic_packet_id="lp",
        suggested_intent="speak",
        max_chars=300,
        max_duration_ms=10000,
        priority=0,
        expires_at_ms=99,
        role="SEER",
        role_strategy=None,
        alive_seats=((2, "Jonas"), (6, "Setsu")),
        dead_seats=(),
    )
    logic = LogicPacket(
        ts=1,
        trace_id="t",
        packet_id="lp",
        phase_id=request.phase_id,
        recipient_npc_id="npc",
        public_state_summary="",
        expires_at_ms=99,
    )

    result = await service.respond(
        logic=logic, request=request, now_ms=2,
    )

    assert result.claimed_seer_result == ClaimedSeerResult(
        target_seat=6, is_wolf=False,
    )
    assert result.claimed_medium_result is None


async def test_speech_service_drops_self_claim() -> None:
    """A wolf NPC that names its own seat as the divined target is
    self-incriminating gibberish; the service drops the structured
    claim before persisting (the speech itself still goes through)."""
    from wolfbot.npc.speech_service import FakeNpcGenerator

    speech = NpcGeneratedSpeech(
        text="自分を占いました（バグ）",
        intent="speak",
        used_logic_ids=(),
        estimated_duration_ms=2000,
        co_declaration="seer",
        claimed_seer_target_seat=2,  # same as speaker_seat below
        claimed_seer_is_wolf=False,
    )
    gen = FakeNpcGenerator(default=speech)
    service = NpcSpeechService(gen)

    request = SpeakRequest(
        ts=1,
        trace_id="t",
        request_id="rq2",
        phase_id="g::day1::DAY_DISCUSSION::1",
        npc_id="npc",
        seat_no=2,
        logic_packet_id="lp",
        suggested_intent="speak",
        max_chars=300,
        max_duration_ms=10000,
        priority=0,
        expires_at_ms=99,
        role="SEER",
        role_strategy=None,
        alive_seats=((2, "Jonas"),),
        dead_seats=(),
    )
    logic = LogicPacket(
        ts=1,
        trace_id="t",
        packet_id="lp",
        phase_id=request.phase_id,
        recipient_npc_id="npc",
        public_state_summary="",
        expires_at_ms=99,
    )

    result = await service.respond(
        logic=logic, request=request, now_ms=2,
    )

    assert result.claimed_seer_result is None  # self-target dropped
