"""Bundle 6: voice-ingest worker — Protocol-level coverage.

Verifies the boundary semantics that matter most for the speech-event-bus:

* NPC packet → discarded at receive boundary (no VAD lifecycle).
* Human packet through full pipeline → exactly one `speech_event_payload`.
* Below-threshold STT result → no `speech_event_payload`, one `stt_failed`.
* SttProviderError → no `speech_event_payload`, one `stt_failed`.
* `apply_snapshot` / `apply_update` from Master correctly maintain the
  voice-ingest registry view.
* Restart abandons open VAD windows.
"""

from __future__ import annotations

from wolfbot.master.voice.stt_service import (
    FakeSttService,
    SttProviderError,
    SttResult,
)
from wolfbot.master.voice.voice_ingest_client import (
    FakeMasterIngestionClient,
    InMemoryNpcRegistryView,
    make_default_listeners,
)
from wolfbot.master.voice.voice_ingest_service import (
    VoiceIngestConfig,
    VoiceIngestService,
)


def _phase_lookup_active() -> tuple[str, str] | None:
    return ("g1", "g1::day1::DAY_DISCUSSION::1")


def _seat_lookup(uid: str) -> int | None:
    table = {"u3": 3, "u4": 4}
    return table.get(uid)


async def test_npc_packet_dropped_at_receive_boundary() -> None:
    view = InMemoryNpcRegistryView()
    view.apply_snapshot(("npc-bot",))
    client = FakeMasterIngestionClient()
    stt = FakeSttService()
    svc = VoiceIngestService(
        registry_view=view,
        master_client=client,
        stt=stt,
        seat_lookup=_seat_lookup,
        phase_lookup=_phase_lookup_active,
        now_ms=lambda: 1000,
    )
    forwarded = await svc.handle_voice_packet(speaker_user_id="npc-bot", pcm=b"x" * 32)
    assert forwarded is False
    assert svc.dropped_npc_packets == 1
    seg_id = await svc.begin_segment(speaker_user_id="npc-bot")
    assert seg_id is None
    assert client.vad_started == []


async def test_human_segment_full_pipeline_emits_speech_event_payload() -> None:
    view = InMemoryNpcRegistryView()
    client = FakeMasterIngestionClient()
    stt = FakeSttService(scripted=[SttResult(text="こんにちは", confidence=0.85, duration_ms=600)])
    svc = VoiceIngestService(
        registry_view=view,
        master_client=client,
        stt=stt,
        seat_lookup=_seat_lookup,
        phase_lookup=_phase_lookup_active,
        now_ms=lambda: 1000,
    )
    seg_id = await svc.begin_segment(speaker_user_id="u3")
    assert seg_id is not None
    await svc.handle_voice_packet(speaker_user_id="u3", pcm=b"audio")
    await svc.end_segment(speaker_user_id="u3")
    assert len(client.vad_started) == 1
    assert len(client.vad_ended) == 1
    assert len(client.speech_payloads) == 1
    payload = client.speech_payloads[0]
    assert payload.text == "こんにちは"
    assert payload.seat_no == 3
    assert payload.segment_id == seg_id
    assert client.stt_failures == []


async def test_low_confidence_drop_does_not_emit_speech_event_payload() -> None:
    view = InMemoryNpcRegistryView()
    client = FakeMasterIngestionClient()
    stt = FakeSttService(scripted=[SttResult(text="あ", confidence=0.3, duration_ms=200)])
    svc = VoiceIngestService(
        registry_view=view,
        master_client=client,
        stt=stt,
        seat_lookup=_seat_lookup,
        phase_lookup=_phase_lookup_active,
        config=VoiceIngestConfig(confidence_threshold=0.6),
        now_ms=lambda: 1,
    )
    await svc.begin_segment(speaker_user_id="u3")
    await svc.end_segment(speaker_user_id="u3")
    assert client.speech_payloads == []
    assert len(client.stt_failures) == 1
    assert client.stt_failures[0].failure_reason == "stt_low_confidence"
    assert svc.stt_low_confidence_count == 1


async def test_stt_provider_error_drops_segment() -> None:
    view = InMemoryNpcRegistryView()
    client = FakeMasterIngestionClient()
    stt = FakeSttService(scripted=[SttProviderError("stt_timeout")])
    svc = VoiceIngestService(
        registry_view=view,
        master_client=client,
        stt=stt,
        seat_lookup=_seat_lookup,
        phase_lookup=_phase_lookup_active,
        now_ms=lambda: 1,
    )
    await svc.begin_segment(speaker_user_id="u3")
    await svc.end_segment(speaker_user_id="u3")
    assert client.speech_payloads == []
    assert len(client.stt_failures) == 1
    assert client.stt_failures[0].failure_reason == "stt_timeout"


async def test_unknown_speaker_seat_skipped() -> None:
    """A speaker that is not in any seat (orphan voice in VC) cannot start a segment."""
    view = InMemoryNpcRegistryView()
    client = FakeMasterIngestionClient()
    stt = FakeSttService(default=SttResult(text="hi", confidence=0.9, duration_ms=1))
    svc = VoiceIngestService(
        registry_view=view,
        master_client=client,
        stt=stt,
        seat_lookup=lambda _uid: None,
        phase_lookup=_phase_lookup_active,
        now_ms=lambda: 1,
    )
    seg = await svc.begin_segment(speaker_user_id="ghost")
    assert seg is None
    assert client.vad_started == []


async def test_inactive_phase_skips_segment() -> None:
    view = InMemoryNpcRegistryView()
    client = FakeMasterIngestionClient()
    stt = FakeSttService(default=SttResult(text="hi", confidence=0.9, duration_ms=1))
    svc = VoiceIngestService(
        registry_view=view,
        master_client=client,
        stt=stt,
        seat_lookup=_seat_lookup,
        phase_lookup=lambda: None,
        now_ms=lambda: 1,
    )
    seg = await svc.begin_segment(speaker_user_id="u3")
    assert seg is None
    assert client.vad_started == []


async def test_registry_view_listeners_apply_snapshot_and_update() -> None:
    view = InMemoryNpcRegistryView()
    on_snap, on_update = make_default_listeners(view)
    on_snap(("a", "b"))
    assert view.npc_user_ids() == {"a", "b"}
    on_update(("c",), ("a",))
    assert view.npc_user_ids() == {"b", "c"}
    assert view.is_npc("c") is True
    assert view.is_npc("a") is False


async def test_restart_abandons_open_segments() -> None:
    view = InMemoryNpcRegistryView()
    client = FakeMasterIngestionClient()
    stt = FakeSttService(default=SttResult(text="x", confidence=0.9, duration_ms=1))
    svc = VoiceIngestService(
        registry_view=view,
        master_client=client,
        stt=stt,
        seat_lookup=_seat_lookup,
        phase_lookup=_phase_lookup_active,
        now_ms=lambda: 1,
    )
    await svc.begin_segment(speaker_user_id="u3")
    await svc.begin_segment(speaker_user_id="u4")
    abandoned = await svc.abandon_open_segments()
    assert abandoned == 2


async def test_pre_stt_silence_gate_skips_short_or_quiet_segments() -> None:
    """Pre-STT silence gate must suppress the STT call for buffers
    too short or too quiet to plausibly contain speech, and emit a
    canonical ``stt_failed reason=pre_stt_silence_gate`` instead."""

    view = InMemoryNpcRegistryView()
    client = FakeMasterIngestionClient()
    # Scripted result is never reached when the gate fires - if it
    # were, the assertion on stt_failures would still catch the
    # mis-routing because confidence=0.99 would land in
    # speech_payloads, not stt_failures.
    stt = FakeSttService(default=SttResult(text="x", confidence=0.99, duration_ms=1))
    svc = VoiceIngestService(
        registry_view=view,
        master_client=client,
        stt=stt,
        seat_lookup=_seat_lookup,
        phase_lookup=_phase_lookup_active,
        config=VoiceIngestConfig(
            pre_stt_min_rms=200,
            pre_stt_min_duration_ms=300,
        ),
        now_ms=lambda: 1,
    )
    # Buffer = 100 frames * 4 bytes (stereo s16) = 400 bytes ≈ 2ms
    # well under both thresholds and contains all-zero PCM.
    await svc.begin_segment(speaker_user_id="u3")
    await svc.handle_voice_packet(speaker_user_id="u3", pcm=b"\x00" * 400)
    await svc.end_segment(speaker_user_id="u3")
    assert client.speech_payloads == []
    assert len(client.stt_failures) == 1
    assert client.stt_failures[0].failure_reason == "pre_stt_silence_gate"
    assert svc.pre_stt_silence_gated_count == 1
    # Sanity: the STT service was never called.
    assert stt.call_count == 0


async def test_pre_stt_silence_gate_passes_loud_speech() -> None:
    """A long, loud buffer should pass the gate and reach STT."""

    view = InMemoryNpcRegistryView()
    client = FakeMasterIngestionClient()
    stt = FakeSttService(default=SttResult(text="ok", confidence=0.9, duration_ms=500))
    svc = VoiceIngestService(
        registry_view=view,
        master_client=client,
        stt=stt,
        seat_lookup=_seat_lookup,
        phase_lookup=_phase_lookup_active,
        config=VoiceIngestConfig(
            pre_stt_min_rms=200,
            pre_stt_min_duration_ms=300,
        ),
        now_ms=lambda: 1,
    )
    # Build ~1s of loud square-wave PCM: 48000 Hz * 2ch * 2B = 192000
    # bytes/sec. Use a +/- 8000 amplitude so RMS clears the 200 gate.
    import struct

    samples = [8000 if (i // 480) % 2 == 0 else -8000 for i in range(48_000 * 2)]
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    await svc.begin_segment(speaker_user_id="u3")
    await svc.handle_voice_packet(speaker_user_id="u3", pcm=pcm)
    await svc.end_segment(speaker_user_id="u3")
    assert len(client.speech_payloads) == 1
    assert client.stt_failures == []
    assert svc.pre_stt_silence_gated_count == 0


async def test_roster_lookup_is_forwarded_to_stt_transcribe() -> None:
    """When ``roster_lookup`` is supplied, its output must reach the
    ``SttService.transcribe`` call so the analyzer LLM can ground
    ``addressed_name`` on real seat names. ``FakeSttService`` records
    the most recent ``roster`` for the assertion."""

    view = InMemoryNpcRegistryView()
    client = FakeMasterIngestionClient()
    stt = FakeSttService(default=SttResult(text="hi", confidence=0.9, duration_ms=1))
    captured_roster: list[tuple[int, str]] = [(3, "🦋ラキオ"), (4, "🌙セツ")]
    svc = VoiceIngestService(
        registry_view=view,
        master_client=client,
        stt=stt,
        seat_lookup=_seat_lookup,
        phase_lookup=_phase_lookup_active,
        roster_lookup=lambda: captured_roster,
        now_ms=lambda: 1,
    )
    await svc.begin_segment(speaker_user_id="u3")
    await svc.handle_voice_packet(speaker_user_id="u3", pcm=b"\x00" * 16)
    await svc.end_segment(speaker_user_id="u3")
    assert stt.last_roster == captured_roster


async def test_roster_lookup_failure_does_not_break_stt_call() -> None:
    """A roster_lookup that raises must not abort STT — the call
    should fall back to a None roster (legacy un-grounded prompt)."""

    view = InMemoryNpcRegistryView()
    client = FakeMasterIngestionClient()
    stt = FakeSttService(default=SttResult(text="hi", confidence=0.9, duration_ms=1))

    def boom() -> list[tuple[int, str]]:
        raise RuntimeError("game state not loaded")

    svc = VoiceIngestService(
        registry_view=view,
        master_client=client,
        stt=stt,
        seat_lookup=_seat_lookup,
        phase_lookup=_phase_lookup_active,
        roster_lookup=boom,
        now_ms=lambda: 1,
    )
    await svc.begin_segment(speaker_user_id="u3")
    await svc.end_segment(speaker_user_id="u3")
    assert stt.call_count == 1
    assert stt.last_roster is None
    assert len(client.speech_payloads) == 1


async def test_pre_stt_silence_gate_disabled_by_default() -> None:
    """Default ``VoiceIngestConfig`` keeps the gate off so existing
    tests / callers that pass tiny buffers still reach STT."""

    view = InMemoryNpcRegistryView()
    client = FakeMasterIngestionClient()
    stt = FakeSttService(default=SttResult(text="x", confidence=0.9, duration_ms=1))
    svc = VoiceIngestService(
        registry_view=view,
        master_client=client,
        stt=stt,
        seat_lookup=_seat_lookup,
        phase_lookup=_phase_lookup_active,
        now_ms=lambda: 1,
    )
    await svc.begin_segment(speaker_user_id="u3")
    await svc.handle_voice_packet(speaker_user_id="u3", pcm=b"\x00" * 16)
    await svc.end_segment(speaker_user_id="u3")
    assert len(client.speech_payloads) == 1
    assert svc.pre_stt_silence_gated_count == 0
