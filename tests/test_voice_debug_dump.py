"""Tests for the optional voice-segment debug dump.

Verifies the on/off toggle, file naming, the .wav being a real WAV
(playable in any audio tool), the .txt sidecar's human-readable
contents, and behaviour on the four STT outcome paths (success /
low-confidence / hard-fail / unexpected exception). Also asserts that
write failures don't propagate to the caller — a flaky disk must
never break the voice path.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from wolfbot.master.stt_service import SttResult
from wolfbot.master.voice_debug_dump import (
    SegmentDumpRecord,
    debug_dir,
    dump_segment,
)


def _record(
    *,
    game_id: str = "g_dbg",
    segment_id: str = "seg_001",
    seat_no: int = 1,
    audio_bytes: int = 9_600,
    result: SttResult | None = None,
    failure_reason: str | None = None,
) -> SegmentDumpRecord:
    return SegmentDumpRecord(
        game_id=game_id,
        phase_id=f"{game_id}::day1::DAY_DISCUSSION::1",
        segment_id=segment_id,
        seat_no=seat_no,
        speaker_user_id="753109683971293296",
        audio_start_ms=1_700_000_000_000,
        audio_end_ms=1_700_000_001_000,
        pcm_sample_rate=48_000,
        pcm_channels=2,
        pcm_sample_width=2,
        audio_bytes=audio_bytes,
        result=result,
        failure_reason=failure_reason,
    )


@pytest.fixture
def enabled_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WOLFBOT_VOICE_DEBUG_DIR", str(tmp_path))
    return tmp_path


async def test_disabled_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the env var, ``dump_segment`` must touch nothing — the
    call site is unconditional, so any disk I/O here would land in
    production logs."""
    monkeypatch.delenv("WOLFBOT_VOICE_DEBUG_DIR", raising=False)
    assert debug_dir() is None
    await dump_segment(_record(), pcm=b"\x00" * 4_000)
    assert list(tmp_path.iterdir()) == []


async def test_writes_wav_and_txt_pair_on_success(enabled_dir: Path) -> None:
    """Both files land under ``<dir>/<game_id>/`` with the segment id as
    a shared stem, so an operator scanning Finder sees ``.wav`` /
    ``.txt`` next to each other."""
    pcm = b"\x01\x02\x03\x04" * 1_000  # 4 KB ≈ 20 ms @ 48 kHz stereo
    result = SttResult(
        text="席3が怪しい",
        confidence=0.92,
        duration_ms=21,
        summary="席3への投票表明",
        co_declaration=None,
        addressed_name=None,
    )
    await dump_segment(_record(result=result), pcm=pcm)

    wav_path = enabled_dir / "g_dbg" / "seg_001.wav"
    txt_path = enabled_dir / "g_dbg" / "seg_001.txt"
    assert wav_path.exists() and txt_path.exists()


async def test_dumped_wav_is_a_real_wav_file(enabled_dir: Path) -> None:
    """A reader using stdlib ``wave`` must accept the file — confirms
    the WAV header isn't garbage and the format matches what we wrote."""
    pcm = b"\x00\x01" * 9_600  # ~50 ms @ 48 kHz stereo
    await dump_segment(_record(audio_bytes=len(pcm)), pcm=pcm)

    wav_path = enabled_dir / "g_dbg" / "seg_001.wav"
    with wave.open(str(wav_path), "rb") as r:
        assert r.getframerate() == 48_000
        assert r.getnchannels() == 2
        assert r.getsampwidth() == 2
        # frames = bytes / (channels * sample_width); for our 19_200 byte
        # blob that's 19_200 / 4 = 4_800.
        assert r.getnframes() == len(pcm) // 4


async def test_txt_leads_with_transcript_for_quick_inspection(
    enabled_dir: Path,
) -> None:
    """Operator's first line of business is "did Whisper hear me say
    X?" — keep the answer at the top of the file."""
    result = SttResult(
        text="占いCO 席7白",
        confidence=0.95,
        duration_ms=2_000,
        summary="占いCO",
        co_declaration="seer",
        addressed_name=None,
    )
    await dump_segment(_record(result=result), pcm=b"\x00" * 4_000)
    body = (enabled_dir / "g_dbg" / "seg_001.txt").read_text(encoding="utf-8")
    first = body.splitlines()[0]
    assert first == "transcript: 占いCO 席7白"
    assert "asr_conf     : 0.950" in body
    assert "co_declaration: seer" in body


async def test_low_confidence_path_dumps_with_failure_reason(
    enabled_dir: Path,
) -> None:
    """A low-confidence segment carries BOTH the (suspicious) transcript
    and the failure reason — operator wants to listen to the audio and
    see whether Whisper hallucinated, and the gate that suppressed the
    speech event needs to be visible."""
    result = SttResult(
        text="ご視聴ありがとうございました",
        confidence=0.05,
        duration_ms=500,
        summary=None,
        co_declaration=None,
        addressed_name=None,
    )
    await dump_segment(
        _record(result=result, failure_reason="stt_low_confidence"),
        pcm=b"\x00" * 4_000,
    )
    body = (enabled_dir / "g_dbg" / "seg_001.txt").read_text(encoding="utf-8")
    assert "transcript: ご視聴ありがとうございました" in body
    assert "asr_conf     : 0.050" in body


async def test_hard_failure_path_dumps_audio_with_failure_reason(
    enabled_dir: Path,
) -> None:
    """When STT itself errored we have no transcript, but the audio is
    the most valuable artifact — let the operator listen to it and
    confirm what the upstream provider was rejecting."""
    await dump_segment(
        _record(result=None, failure_reason="groq_http_400"),
        pcm=b"\x00" * 4_000,
    )
    body = (enabled_dir / "g_dbg" / "seg_001.txt").read_text(encoding="utf-8")
    first = body.splitlines()[0]
    assert first == "transcript: <FAILED: groq_http_400>"
    assert "failure_reason: groq_http_400" in body


async def test_path_components_sanitized(enabled_dir: Path) -> None:
    """Game IDs and segment IDs come from external sources; a malicious
    or buggy id must not escape the debug directory."""
    rec = _record(game_id="../escape/../danger", segment_id="../etc/passwd")
    await dump_segment(rec, pcm=b"\x00" * 100)
    # Nothing should have been written outside our tmp dir
    assert not (enabled_dir.parent / "escape").exists()
    # And the sanitized files DO exist somewhere under our tmp dir
    found = list(enabled_dir.rglob("*.wav"))
    assert len(found) == 1
    assert ".." not in str(found[0])


async def test_write_failure_swallowed(
    enabled_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flaky disk (or read-only mount) must never break the voice
    path — the dump is best-effort observability only."""
    import wolfbot.master.voice_debug_dump as mod

    def boom(*_a: object, **_kw: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_write_pair", boom)
    # Should not raise.
    await dump_segment(_record(), pcm=b"\x00" * 100)


async def test_overrides_pcm_format_for_downsampled_callers(
    enabled_dir: Path,
) -> None:
    """A future caller that down-mixes to 16 kHz mono before dump can
    pass non-default format kwargs and the WAV reflects them."""
    rec = SegmentDumpRecord(
        game_id="g_dbg",
        phase_id="g_dbg::day1::DAY_DISCUSSION::1",
        segment_id="seg_mono",
        seat_no=1,
        speaker_user_id="u",
        audio_start_ms=0,
        audio_end_ms=1_000,
        pcm_sample_rate=16_000,
        pcm_channels=1,
        pcm_sample_width=2,
        audio_bytes=32_000,
        result=None,
    )
    await dump_segment(rec, pcm=b"\x00" * 32_000)
    with wave.open(str(enabled_dir / "g_dbg" / "seg_mono.wav"), "rb") as r:
        assert r.getframerate() == 16_000
        assert r.getnchannels() == 1


async def test_dumped_txt_is_human_readable_not_json(enabled_dir: Path) -> None:
    """Format is plain ``key: value`` lines, not JSON — operator-friendly
    when opened in a viewer that doesn't pretty-print JSON. The first
    line is always ``transcript: ...`` (or a clear failure marker)."""
    await dump_segment(
        _record(
            result=SttResult(
                text="hi",
                confidence=1.0,
                duration_ms=100,
            ),
        ),
        pcm=b"\x00" * 100,
    )
    body = (enabled_dir / "g_dbg" / "seg_001.txt").read_text(encoding="utf-8")
    # No JSON braces should appear at the top level.
    assert not body.startswith("{")
    # The structure is colon-delimited key/value lines.
    assert "transcript: hi" in body
    assert "audio_window : " in body
