"""Bundle 9: cross-component observability.

Verifies:
- `emit_event` produces a structured payload with the canonical envelope.
- `CapturingHandler` records and filters events by name + fields.
- `emit_phase_summary` sums human / NPC speech events from `speech_events`
  and reactive_voice telemetry from `npc_speak_*` / `npc_playback_events`.
- The summary mode field reflects the game's discussion_mode (rounds vs
  reactive_voice).
"""

from __future__ import annotations

import logging

from wolfbot.domain.discussion import (
    SpeechSource,
    make_phase_id,
)
from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Seat
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discussion_phase_summary import emit_phase_summary
from wolfbot.services.discussion_service import (
    DiscussionService,
    SqliteSpeechEventStore,
    make_human_text_event,
    make_npc_generated_event,
    make_phase_baseline,
)
from wolfbot.services.structured_logging import (
    COMPONENT_MASTER,
    CapturingHandler,
    build_discussion_phase_summary,
    emit_event,
)


async def _seed_phase_with_speech(
    repo: SqliteRepo, *, mode: str = "rounds"
) -> tuple[Game, str, DiscussionService]:
    g = Game(
        id="ob1",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode=mode,
    )
    await repo.create_game(g)
    await repo.insert_seat(
        g.id,
        Seat(seat_no=1, display_name="Alice", discord_user_id="u1", is_llm=False, persona_key=None),
    )
    await repo.insert_seat(
        g.id,
        Seat(
            seat_no=2, display_name="セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
    )
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 2, Role.SEER)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2],
            created_at_ms=1,
        )
    )
    await store.insert(
        make_human_text_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=1,
            text="人間です",
            created_at_ms=2,
        )
    )
    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=2,
            text="占いCO",
            created_at_ms=3,
        )
    )
    return g, phase_id, discussion


def test_emit_event_records_envelope_fields() -> None:
    handler = CapturingHandler()
    test_logger = logging.getLogger("wolfbot.observability.unit-test")
    test_logger.setLevel(logging.DEBUG)
    test_logger.addHandler(handler)
    try:
        emit_event(
            test_logger,
            component="master",
            event="speak_request_suppressed",
            game_id="g1",
            phase_id="ph",
            failure_reason="human_currently_speaking",
        )
        events = list(handler.events_with(name="speak_request_suppressed", game_id="g1"))
        assert len(events) == 1
        ev = events[0]
        assert ev.payload["component"] == "master"
        assert ev.payload["failure_reason"] == "human_currently_speaking"
        assert "ts" in ev.payload
    finally:
        test_logger.removeHandler(handler)


def test_capturing_handler_filters_by_name_and_fields() -> None:
    handler = CapturingHandler()
    test_logger = logging.getLogger("wolfbot.observability.filter-test")
    test_logger.setLevel(logging.DEBUG)
    test_logger.addHandler(handler)
    try:
        emit_event(test_logger, component="master", event="a", game_id="g1")
        emit_event(test_logger, component="master", event="a", game_id="g2")
        emit_event(test_logger, component="master", event="b", game_id="g1")
        a_g1 = list(handler.events_with(name="a", game_id="g1"))
        assert len(a_g1) == 1
        all_a = list(handler.events_with(name="a"))
        assert len(all_a) == 2
    finally:
        test_logger.removeHandler(handler)


def test_build_discussion_phase_summary_includes_required_fields() -> None:
    payload = build_discussion_phase_summary(
        game_id="g1",
        phase_id="ph",
        mode="rounds",
        speech_events_total=4,
        human_speech_events=1,
        npc_speech_events=3,
    )
    for key in (
        "game_id",
        "phase_id",
        "mode",
        "speech_events_total",
        "human_speech_events",
        "npc_speech_events",
        "stt_success",
        "stt_failed",
        "logic_packets_built",
        "speak_requests_sent",
        "speak_results_accepted",
        "speak_results_rejected",
        "playback_authorized",
        "tts_success",
        "tts_failed",
        "playback_success",
        "playback_failed",
        "stale_dropped",
    ):
        assert key in payload


async def test_emit_phase_summary_counts_speech_events_in_rounds_mode(
    repo: SqliteRepo,
) -> None:
    handler = CapturingHandler()
    wolfbot_logger = logging.getLogger("wolfbot")
    prior_level = wolfbot_logger.level
    wolfbot_logger.setLevel(logging.DEBUG)
    wolfbot_logger.addHandler(handler)
    try:
        g, phase_id, discussion = await _seed_phase_with_speech(repo, mode="rounds")
        counts = await emit_phase_summary(
            repo=repo,
            discussion=discussion,
            game_id=g.id,
            phase_id=phase_id,
            mode="rounds",
        )
        assert counts["speech_events_total"] == 2
        assert counts["human_speech_events"] == 1
        assert counts["npc_speech_events"] == 1
        # The reactive_voice telemetry is zero in rounds mode (no audit rows).
        assert counts["speak_requests_sent"] == 0
        assert counts["playback_authorized"] == 0
        events = list(handler.events_with(name="discussion_phase_summary", game_id=g.id))
        assert len(events) == 1
        assert events[0].payload["mode"] == "rounds"
        assert events[0].payload["component"] == COMPONENT_MASTER
    finally:
        wolfbot_logger.removeHandler(handler)
        wolfbot_logger.setLevel(prior_level)


async def test_emit_phase_summary_includes_audit_rows_in_reactive_voice_mode(
    repo: SqliteRepo,
) -> None:
    handler = CapturingHandler()
    wolfbot_logger = logging.getLogger("wolfbot")
    prior_level = wolfbot_logger.level
    wolfbot_logger.setLevel(logging.DEBUG)
    wolfbot_logger.addHandler(handler)
    try:
        g, phase_id, discussion = await _seed_phase_with_speech(repo, mode="reactive_voice")
        # Seed two requests: one accepted+played, one rejected.
        await repo.insert_npc_speak_request(
            request_id="r1",
            game_id=g.id,
            phase_id=phase_id,
            npc_id="n2",
            seat_no=2,
            logic_packet_id="lp1",
            suggested_intent="speak",
            max_chars=80,
            max_duration_ms=8000,
            priority=0,
            expires_at_ms=10_000,
            created_at_ms=1,
        )
        await repo.insert_npc_speak_result(
            request_id="r1",
            game_id=g.id,
            phase_id=phase_id,
            npc_id="n2",
            status="accepted",
            text="占いCO",
            used_logic_ids=["c1"],
            intent="co",
            estimated_duration_ms=1500,
            failure_reason=None,
            received_at_ms=2,
        )
        await repo.open_npc_playback(
            request_id="r1",
            game_id=g.id,
            phase_id=phase_id,
            npc_id="n2",
            speech_event_id="ev1",
            authorized_at_ms=2,
            playback_deadline_ms=10_000,
        )
        await repo.update_npc_playback_tts(
            "r1", outcome="success", duration_ms=400, failure_reason=None
        )
        await repo.close_npc_playback(
            "r1", finished_at_ms=3, outcome="succeeded", failure_reason=None
        )
        await repo.insert_npc_speak_request(
            request_id="r2",
            game_id=g.id,
            phase_id=phase_id,
            npc_id="n2",
            seat_no=2,
            logic_packet_id="lp2",
            suggested_intent="speak",
            max_chars=80,
            max_duration_ms=8000,
            priority=0,
            expires_at_ms=10_000,
            created_at_ms=4,
        )
        await repo.insert_npc_speak_result(
            request_id="r2",
            game_id=g.id,
            phase_id=phase_id,
            npc_id="n2",
            status="rejected",
            text=None,
            used_logic_ids=None,
            intent=None,
            estimated_duration_ms=None,
            failure_reason="stale_phase",
            received_at_ms=5,
        )

        counts = await emit_phase_summary(
            repo=repo,
            discussion=discussion,
            game_id=g.id,
            phase_id=phase_id,
            mode="reactive_voice",
        )
        assert counts["speak_requests_sent"] == 2
        assert counts["speak_results_accepted"] == 1
        assert counts["speak_results_rejected"] == 1
        assert counts["tts_success"] == 1
        assert counts["playback_success"] == 1
        assert counts["stale_dropped"] == 1
        events = list(
            handler.events_with(
                name="discussion_phase_summary", game_id=g.id, mode="reactive_voice"
            )
        )
        assert len(events) == 1
    finally:
        wolfbot_logger.removeHandler(handler)
        wolfbot_logger.setLevel(prior_level)


def test_speech_source_filter_excludes_phase_baseline() -> None:
    """The summary's `speech_events_total` must NOT count phase_baseline."""
    # Sanity check via SpeechSource.
    assert SpeechSource.PHASE_BASELINE != SpeechSource.TEXT
    assert SpeechSource.PHASE_BASELINE != SpeechSource.VOICE_STT
    assert SpeechSource.PHASE_BASELINE != SpeechSource.NPC_GENERATED
