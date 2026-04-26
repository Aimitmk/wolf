"""Bundle 7: NPC voice worker — integration coverage.

Drives the `NpcClient` directly with synthetic JSON envelopes. Substitutes
`FakeNpcGenerator`, `FakeTtsService`, and `FakeVoicePlayback`. Verifies:

- Successful end-to-end: SpeakRequest → SpeakResult → PlaybackAuthorized →
  TTS synthesis → playback → tts_finished + playback_finished.
- Decline path: empty generator output → SpeakResult(declined).
- Pre-authorization invariant: no audio is played before
  PlaybackAuthorized.
- Unauthorized: PlaybackRejected drops the queued utterance silently.
- TTS error: tts_failed message; no playback.
- Playback error: playback_failed message after tts_finished.
- TtsCache: identical text re-uses synthesized audio.
- Logic packet routing: SpeakRequest with unknown packet still produces a
  SpeakResult (best-effort).
"""

from __future__ import annotations

import json

from wolfbot.domain.ws_messages import (
    LogicPacket,
    NpcRegistered,
    PlaybackAuthorized,
    PlaybackFailed,
    PlaybackFinished,
    PlaybackRejected,
    SpeakRequest,
    SpeakResult,
    TtsFinished,
)
from wolfbot.services.npc_client import NpcClient, NpcClientConfig
from wolfbot.services.npc_speech_service import (
    FakeNpcGenerator,
    NpcGeneratedSpeech,
    NpcSpeechService,
)
from wolfbot.services.tts_service import (
    FakeTtsService,
    InMemoryTtsCache,
    TtsProviderError,
    TtsResult,
)
from wolfbot.services.voice_playback_service import (
    FakeVoicePlayback,
    VoicePlaybackError,
)


def _make_client(
    *,
    generator: FakeNpcGenerator,
    tts: FakeTtsService,
    playback: FakeVoicePlayback,
    captured: list[str] | None = None,
    cache: InMemoryTtsCache | None = None,
) -> tuple[NpcClient, list[str]]:
    out = captured if captured is not None else []

    async def send(msg: str) -> None:
        out.append(msg)

    speech = NpcSpeechService(generator)
    client = NpcClient(
        config=NpcClientConfig(
            npc_id="npc_p2",
            discord_bot_user_id="bot2",
            voice_id="ja-Standard-A",
        ),
        speech=speech,
        tts=tts,
        playback=playback,
        send=send,
        now_ms=lambda: 1000,
        cache=cache or InMemoryTtsCache(max_entries=4),
    )
    return client, out


def _make_logic(packet_id: str = "lp1", phase_id: str = "ph") -> LogicPacket:
    return LogicPacket(
        ts=1,
        trace_id="t",
        packet_id=packet_id,
        phase_id=phase_id,
        recipient_npc_id="npc_p2",
        public_state_summary="silent_seats=[]",
        logic_candidates=(),
        pressure={},
        expires_at_ms=2000,
    )


def _make_request(
    *, request_id: str = "sr1", logic_packet_id: str = "lp1", phase_id: str = "ph"
) -> SpeakRequest:
    return SpeakRequest(
        ts=1,
        trace_id="t",
        request_id=request_id,
        phase_id=phase_id,
        npc_id="npc_p2",
        seat_no=2,
        logic_packet_id=logic_packet_id,
        suggested_intent="speak",
        max_chars=80,
        max_duration_ms=8000,
        priority=0,
        expires_at_ms=2000,
    )


async def test_end_to_end_authorized_playback_finishes() -> None:
    gen = FakeNpcGenerator(
        scripted=[
            NpcGeneratedSpeech(
                text="まあそれは違うよ",
                intent="counter",
                used_logic_ids=("c1",),
                estimated_duration_ms=2000,
            )
        ]
    )
    tts = FakeTtsService(scripted=[TtsResult(audio=b"audio", duration_ms=900)])
    playback = FakeVoicePlayback(started_at_ms=2000, finished_at_ms=2900)
    client, captured = _make_client(generator=gen, tts=tts, playback=playback)

    # Logic packet → speak_request → speak_result.
    await client.process_message(_make_logic().model_dump_json())
    await client.process_message(_make_request().model_dump_json())
    result = SpeakResult.model_validate_json(captured[-1])
    assert result.status == "accepted" and result.text == "まあそれは違うよ"

    # No playback yet.
    assert playback.plays == []

    # Authorize.
    auth = PlaybackAuthorized(
        ts=2,
        trace_id="t",
        request_id="sr1",
        npc_id="npc_p2",
        speech_event_id="ev1",
        playback_deadline_ms=10_000,
    )
    await client.process_message(auth.model_dump_json())
    # tts_finished + playback_finished sent in order.
    assert any(json.loads(m).get("type") == "tts_finished" for m in captured)
    assert any(json.loads(m).get("type") == "playback_finished" for m in captured)
    finished = next(
        PlaybackFinished.model_validate_json(m)
        for m in captured
        if json.loads(m).get("type") == "playback_finished"
    )
    assert finished.started_at_ms == 2000
    assert finished.finished_at_ms == 2900
    assert playback.plays == [(b"audio", 48_000)]


async def test_decline_when_generator_returns_none() -> None:
    gen = FakeNpcGenerator(scripted=[None])
    tts = FakeTtsService()
    playback = FakeVoicePlayback()
    client, captured = _make_client(generator=gen, tts=tts, playback=playback)
    await client.process_message(_make_logic().model_dump_json())
    await client.process_message(_make_request().model_dump_json())
    result = SpeakResult.model_validate_json(captured[-1])
    assert result.status == "declined"


async def test_no_playback_before_authorization() -> None:
    """Without a PlaybackAuthorized, no playback / TTS event is emitted —
    matching the discord-integration spec."""
    gen = FakeNpcGenerator(
        scripted=[
            NpcGeneratedSpeech(
                text="こんにちは", intent="greeting", used_logic_ids=(), estimated_duration_ms=500
            )
        ]
    )
    tts = FakeTtsService(scripted=[TtsResult(audio=b"abc", duration_ms=500)])
    playback = FakeVoicePlayback()
    client, captured = _make_client(generator=gen, tts=tts, playback=playback)
    await client.process_message(_make_logic().model_dump_json())
    await client.process_message(_make_request().model_dump_json())
    # No tts_finished / playback_finished yet.
    assert playback.plays == []
    types = [json.loads(m).get("type") for m in captured]
    assert "tts_finished" not in types
    assert "playback_finished" not in types


async def test_playback_rejected_drops_pending_silently() -> None:
    gen = FakeNpcGenerator(
        scripted=[
            NpcGeneratedSpeech(
                text="占いCO", intent="co", used_logic_ids=(), estimated_duration_ms=800
            )
        ]
    )
    tts = FakeTtsService(scripted=[TtsResult(audio=b"abc", duration_ms=500)])
    playback = FakeVoicePlayback()
    client, _ = _make_client(generator=gen, tts=tts, playback=playback)
    await client.process_message(_make_logic().model_dump_json())
    await client.process_message(_make_request().model_dump_json())
    rej = PlaybackRejected(
        ts=2,
        trace_id="t",
        request_id="sr1",
        npc_id="npc_p2",
        failure_reason="utterance_too_long",
    )
    await client.process_message(rej.model_dump_json())
    # Subsequent authorization MUST NOT replay (because the result was rejected).
    auth = PlaybackAuthorized(
        ts=3,
        trace_id="t",
        request_id="sr1",
        npc_id="npc_p2",
        speech_event_id="ev1",
        playback_deadline_ms=10_000,
    )
    await client.process_message(auth.model_dump_json())
    assert playback.plays == []


async def test_tts_failure_emits_tts_failed_and_no_playback() -> None:
    gen = FakeNpcGenerator(
        scripted=[
            NpcGeneratedSpeech(
                text="やあ", intent="greeting", used_logic_ids=(), estimated_duration_ms=500
            )
        ]
    )
    tts = FakeTtsService(scripted=[TtsProviderError("tts_provider_error")])
    playback = FakeVoicePlayback()
    client, captured = _make_client(generator=gen, tts=tts, playback=playback)
    await client.process_message(_make_logic().model_dump_json())
    await client.process_message(_make_request().model_dump_json())
    auth = PlaybackAuthorized(
        ts=2,
        trace_id="t",
        request_id="sr1",
        npc_id="npc_p2",
        speech_event_id="ev1",
        playback_deadline_ms=10_000,
    )
    await client.process_message(auth.model_dump_json())
    types = [json.loads(m).get("type") for m in captured]
    assert "tts_failed" in types
    assert "tts_finished" not in types
    assert playback.plays == []


async def test_playback_error_emits_playback_failed() -> None:
    gen = FakeNpcGenerator(
        scripted=[
            NpcGeneratedSpeech(
                text="やあ", intent="greeting", used_logic_ids=(), estimated_duration_ms=500
            )
        ]
    )
    tts = FakeTtsService(scripted=[TtsResult(audio=b"abc", duration_ms=500)])
    playback = FakeVoicePlayback(
        raise_for_audio=VoicePlaybackError("discord_playback_error"),
    )
    client, captured = _make_client(generator=gen, tts=tts, playback=playback)
    await client.process_message(_make_logic().model_dump_json())
    await client.process_message(_make_request().model_dump_json())
    auth = PlaybackAuthorized(
        ts=2,
        trace_id="t",
        request_id="sr1",
        npc_id="npc_p2",
        speech_event_id="ev1",
        playback_deadline_ms=10_000,
    )
    await client.process_message(auth.model_dump_json())
    failed = next(
        PlaybackFailed.model_validate_json(m)
        for m in captured
        if json.loads(m).get("type") == "playback_failed"
    )
    assert failed.failure_reason == "discord_playback_error"


async def test_tts_cache_avoids_resynth() -> None:
    gen = FakeNpcGenerator(
        scripted=[
            NpcGeneratedSpeech(
                text="同じ", intent="speak", used_logic_ids=(), estimated_duration_ms=400
            ),
            NpcGeneratedSpeech(
                text="同じ", intent="speak", used_logic_ids=(), estimated_duration_ms=400
            ),
        ]
    )
    tts = FakeTtsService(default=TtsResult(audio=b"a", duration_ms=400))
    playback = FakeVoicePlayback(started_at_ms=1, finished_at_ms=2)
    cache = InMemoryTtsCache(max_entries=4)
    client, _captured = _make_client(generator=gen, tts=tts, playback=playback, cache=cache)
    await client.process_message(_make_logic().model_dump_json())
    await client.process_message(_make_request(request_id="r1").model_dump_json())
    await client.process_message(
        PlaybackAuthorized(
            ts=1,
            trace_id="t",
            request_id="r1",
            npc_id="npc_p2",
            speech_event_id="ev1",
            playback_deadline_ms=10_000,
        ).model_dump_json()
    )
    await client.process_message(_make_request(request_id="r2").model_dump_json())
    await client.process_message(
        PlaybackAuthorized(
            ts=1,
            trace_id="t",
            request_id="r2",
            npc_id="npc_p2",
            speech_event_id="ev2",
            playback_deadline_ms=10_000,
        ).model_dump_json()
    )
    # First synth uncached; second synth must hit the cache.
    assert cache.hits == 1
    assert cache.misses == 1


async def test_npc_registered_marks_client_registered() -> None:
    gen = FakeNpcGenerator()
    tts = FakeTtsService()
    playback = FakeVoicePlayback()
    client, _ = _make_client(generator=gen, tts=tts, playback=playback)
    assert client.registered is False
    msg = NpcRegistered(
        ts=1, trace_id="t", npc_id="npc_p2", assigned_seat=2, game_id="g", phase_id="ph"
    )
    await client.process_message(msg.model_dump_json())
    assert client.registered is True


async def test_unknown_logic_packet_uses_empty_logic_in_speak_result() -> None:
    """If a SpeakRequest references an unseen `logic_packet_id`, the NPC
    still produces a SpeakResult — degraded, but not silent."""
    gen = FakeNpcGenerator(
        scripted=[
            NpcGeneratedSpeech(
                text="ええと", intent="speak", used_logic_ids=(), estimated_duration_ms=300
            )
        ]
    )
    tts = FakeTtsService()
    playback = FakeVoicePlayback()
    client, captured = _make_client(generator=gen, tts=tts, playback=playback)
    await client.process_message(_make_request(logic_packet_id="never-seen").model_dump_json())
    result = SpeakResult.model_validate_json(captured[-1])
    assert result.status == "accepted"


async def test_invalid_inbound_json_is_logged_not_raised() -> None:
    gen = FakeNpcGenerator()
    tts = FakeTtsService()
    playback = FakeVoicePlayback()
    client, _ = _make_client(generator=gen, tts=tts, playback=playback)
    await client.process_message("{not-json")
    await client.process_message(json.dumps({"no_type_here": True}))
    # No exception — handler must be tolerant.


def test_npc_bot_main_module_loads() -> None:
    """Smoke-load the entrypoint module to catch import-time regressions."""
    import importlib

    mod = importlib.import_module("wolfbot.npc_bot_main")
    assert hasattr(mod, "main")


async def test_tts_finished_includes_correct_duration() -> None:
    gen = FakeNpcGenerator(
        scripted=[
            NpcGeneratedSpeech(
                text="ね", intent="speak", used_logic_ids=(), estimated_duration_ms=10
            )
        ]
    )
    tts = FakeTtsService(scripted=[TtsResult(audio=b"a", duration_ms=750)])
    playback = FakeVoicePlayback(started_at_ms=1, finished_at_ms=2)
    client, captured = _make_client(generator=gen, tts=tts, playback=playback)
    await client.process_message(_make_logic().model_dump_json())
    await client.process_message(_make_request().model_dump_json())
    await client.process_message(
        PlaybackAuthorized(
            ts=2,
            trace_id="t",
            request_id="sr1",
            npc_id="npc_p2",
            speech_event_id="ev1",
            playback_deadline_ms=10_000,
        ).model_dump_json()
    )
    finished = next(
        TtsFinished.model_validate_json(m)
        for m in captured
        if json.loads(m).get("type") == "tts_finished"
    )
    assert finished.tts_duration_ms == 750
