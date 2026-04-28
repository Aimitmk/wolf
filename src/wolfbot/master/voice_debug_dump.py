"""Optional per-segment audio dump for debugging the voice pipeline.

When ``WOLFBOT_VOICE_DEBUG_DIR`` is set, every voice segment Master
processes — successful, low-confidence, or hard-failed — is written
to disk so an operator can:

1. Listen to the raw audio that was sent to Whisper
2. Read the transcript / structured analysis next to it
3. Diagnose hallucinations, dropped segments, mid-segment corruption
   events without needing to re-run a game

Layout::

    $WOLFBOT_VOICE_DEBUG_DIR/
      {game_id}/
        {speaker_name}/
          seg_{id}.wav    # 48 kHz stereo 16-bit (Discord native)
          seg_{id}.txt    # transcript + metadata, paired with the .wav

The speaker subdirectory uses ``display_name`` when known so an operator
can browse the dumps grouped by player; falls back to the Discord user
ID when the display_name isn't available.

Disabled by default — without the env var set, dumping is a no-op so
production deployments can leave the call site in place. File writes
run in a worker thread (``asyncio.to_thread``) so the audio path
never blocks on local disk.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from wolfbot.master.stt_service import SttResult, pcm_to_wav

log = logging.getLogger(__name__)

_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")

# Username sanitizer — preserves unicode (so "🌙 セツ" stays readable)
# and only strips characters that are unsafe on Windows / macOS / Linux
# filesystems or that would create traversal hazards.
_UNSAFE_PATH_CHARS = re.compile(r"[<>:\"/\\|?*\x00-\x1f]+")


@dataclass(frozen=True)
class SegmentDumpRecord:
    """Everything the dump file needs to render a useful debug page.

    ``transcript`` and ``analysis`` are populated on success / partial
    success; ``failure_reason`` carries the canonical voice-ingest
    enum (``stt_provider_error`` / ``stt_low_confidence`` / etc.) when
    the segment didn't make it through. The two are not mutually
    exclusive — a low-confidence segment still has a transcript worth
    inspecting.
    """

    game_id: str
    phase_id: str
    segment_id: str
    seat_no: int
    speaker_user_id: str
    audio_start_ms: int
    audio_end_ms: int
    pcm_sample_rate: int
    pcm_channels: int
    pcm_sample_width: int
    audio_bytes: int
    display_name: str | None = None
    result: SttResult | None = None
    failure_reason: str | None = None


def debug_dir() -> Path | None:
    """Directory configured via ``WOLFBOT_VOICE_DEBUG_DIR``, or ``None``."""
    raw = os.environ.get("WOLFBOT_VOICE_DEBUG_DIR")
    return Path(raw) if raw else None


def _sanitize(s: str) -> str:
    """Sanitize a single path component (game_id or segment_id)."""
    cleaned = _SAFE_RE.sub("_", s).strip("_")
    return cleaned or "x"


def _sanitize_username(name: str | None, fallback: str) -> str:
    """Sanitize a display_name for use as a directory component.

    Lenient compared to :func:`_sanitize` — preserves Japanese / emoji
    so the dump tree stays human-browsable. Replaces only characters
    that are illegal on common filesystems or that would enable path
    traversal. Falls back to ``fallback`` (typically the Discord user
    ID) when the input is empty / None / sanitizes to nothing.
    """
    if not name:
        return _sanitize(fallback)
    # Drop unsafe chars first so traversal segments like "../" can't
    # survive even if the rest of the string is harmless.
    cleaned = _UNSAFE_PATH_CHARS.sub("_", name)
    # Strip leading/trailing dots and whitespace — `.` and ` ` directory
    # names are legal but visually confusing and (on Windows) trimmed
    # silently by the OS, which would collapse two distinct names.
    cleaned = cleaned.strip(" .\t\n\r")
    # Defensive: any residual ".." sequences get neutralised.
    cleaned = cleaned.replace("..", "_")
    # Keep filenames at a sane length — most filesystems cap each
    # component at 255 bytes; UTF-8 Japanese is 3 bytes/char.
    if len(cleaned.encode("utf-8")) > 80:
        cleaned = cleaned[:40]
    return cleaned or _sanitize(fallback)


def _format_txt(record: SegmentDumpRecord) -> str:
    """Build a human-readable .txt sidecar for the matching .wav.

    Plain text rather than JSON so the transcript line is what an
    operator sees first when they open the file in Finder/quick-look —
    they're typically debugging "did Whisper hear me say X?" and the
    answer should not be buried under metadata fields.

    The full analyzer-LLM JSON output is appended at the bottom under
    ``analysis (raw):`` so structured fields like ``vote_target_seat``
    and ``stance`` are visible without cross-referencing the JSONL
    trace. Pretty-printed with ``ensure_ascii=False`` so Japanese
    field values stay readable.
    """
    import json

    duration_s = (record.audio_end_ms - record.audio_start_ms) / 1000.0
    lines: list[str] = []
    if record.result is not None and record.result.text:
        lines.append(f"transcript: {record.result.text}")
    elif record.failure_reason:
        lines.append(f"transcript: <FAILED: {record.failure_reason}>")
    else:
        lines.append("transcript: <EMPTY>")
    lines.append("")
    lines.append(f"game_id      : {record.game_id}")
    lines.append(f"phase_id     : {record.phase_id}")
    lines.append(f"segment_id   : {record.segment_id}")
    lines.append(f"seat_no      : {record.seat_no}")
    lines.append(f"speaker_uid  : {record.speaker_user_id}")
    if record.display_name:
        lines.append(f"speaker_name : {record.display_name}")
    lines.append(f"audio_window : {record.audio_start_ms} → {record.audio_end_ms} ms ({duration_s:.2f}s)")
    lines.append(
        f"pcm_format   : {record.pcm_sample_rate}Hz "
        f"{record.pcm_channels}ch {record.pcm_sample_width * 8}bit"
    )
    lines.append(f"audio_bytes  : {record.audio_bytes}")
    if record.result is not None:
        lines.append(f"asr_conf     : {record.result.confidence:.3f}")
        lines.append(f"duration_ms  : {record.result.duration_ms}")
        if record.result.summary:
            lines.append(f"summary      : {record.result.summary}")
        if record.result.co_declaration:
            lines.append(f"co_declaration: {record.result.co_declaration}")
        if record.result.addressed_name:
            lines.append(f"addressed_name: {record.result.addressed_name}")
    if record.failure_reason and not (
        record.result is not None and record.result.text
    ):
        lines.append(f"failure_reason: {record.failure_reason}")
    if record.result is not None and record.result.raw_analysis:
        lines.append("")
        lines.append("analysis (raw):")
        lines.append(
            json.dumps(record.result.raw_analysis, ensure_ascii=False, indent=2)
        )
    return "\n".join(lines) + "\n"


async def dump_segment(record: SegmentDumpRecord, pcm: bytes) -> None:
    """Write ``seg_<id>.wav`` and ``seg_<id>.txt`` to the debug dir.

    No-op when the debug dir env var is unset, so call sites can leave
    this in place unconditionally. Write failures are logged and
    swallowed — debug dumping must never break the voice path.
    """
    base = debug_dir()
    if base is None:
        return
    try:
        wav = pcm_to_wav(
            pcm,
            sample_rate=record.pcm_sample_rate,
            channels=record.pcm_channels,
            sample_width=record.pcm_sample_width,
        )
        txt = _format_txt(record)
        speaker_dir = _sanitize_username(
            record.display_name, fallback=record.speaker_user_id
        )
        target_dir = base / _sanitize(record.game_id) / speaker_dir
        seg_stem = _sanitize(record.segment_id)
        await asyncio.to_thread(_write_pair, target_dir, seg_stem, wav, txt)
    except Exception:
        log.exception(
            "voice_debug_dump_failed game=%s segment=%s",
            record.game_id,
            record.segment_id,
        )


def _write_pair(target_dir: Path, seg_stem: str, wav: bytes, txt: str) -> None:
    """Synchronous file writer — runs inside ``asyncio.to_thread``."""
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / f"{seg_stem}.wav").write_bytes(wav)
    (target_dir / f"{seg_stem}.txt").write_text(txt, encoding="utf-8")


__all__ = [
    "SegmentDumpRecord",
    "debug_dir",
    "dump_segment",
]
