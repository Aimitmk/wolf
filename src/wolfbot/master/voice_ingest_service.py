"""voice-ingest worker — VAD + STT + Master ingestion.

The worker pipeline:

1. ``handle_voice_packet`` is called for every Discord-side voice packet.
   - If the speaker's `discord_user_id` is in the NPC registry view, the
     packet is discarded at the receive boundary (no VAD, no STT).
2. Otherwise the packet feeds a per-speaker buffer. ``begin_segment`` /
   ``end_segment`` correspond to the VAD lifecycle. ``begin_segment``
   sends `vad_speech_started` to Master and assigns a `segment_id`;
   ``end_segment`` sends `vad_speech_ended` and triggers async STT.
3. STT result handling:
   - Below `confidence_threshold` → drop, log `stt_low_confidence`,
     send `stt_failed` to Master with `failure_reason=stt_low_confidence`.
   - Hard `SttProviderError` → drop, log `stt_request_failed`,
     send `stt_failed` with the provider's failure_reason.
   - Otherwise → send `speech_event_payload` to Master.

VAD itself is provided by an injected `VadEngine` Protocol so unit tests
can replay scripted VAD transitions without driving real audio.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from wolfbot.domain.ws_messages import (
    Heartbeat,
    SpeechEventPayload,
    SttFailed,
    VadSpeechEnded,
    VadSpeechStarted,
)
from wolfbot.master.stt_service import (
    SttProviderError,
    SttResult,
    SttService,
)
from wolfbot.master.voice_ingest_client import (
    MasterIngestionClient,
    NpcRegistryView,
)

log = logging.getLogger(__name__)


@dataclass
class VoiceIngestConfig:
    confidence_threshold: float = 0.6
    stt_timeout_s: float = 10.0
    stt_language: str = "ja-JP"
    vad_finalization_timeout_ms: int = 4000
    heartbeat_interval_s: float = 5.0


@dataclass
class _OpenSegment:
    """Per-speaker open VAD segment, awaiting ``end_segment``."""

    segment_id: str
    seat_no: int
    speaker_user_id: str
    audio_start_ms: int
    audio_buffer: bytearray = field(default_factory=bytearray)


@runtime_checkable
class VadEngine(Protocol):
    """VAD lifecycle events generator. Production VAD is webrtc-based; tests
    drive the lifecycle manually."""

    def push_pcm(self, *, speaker_user_id: str, pcm: bytes) -> tuple[str | None, bytes | None]:
        """Push a PCM frame; return (event, audio) where event ∈
        {None, "started", "ended"} and audio is the buffered audio when
        the segment ends."""
        ...


def _now_ms() -> int:
    return int(time.time() * 1000)


class VoiceIngestService:
    """Single-process voice-ingest orchestrator.

    Has no Discord-specific code itself — wire it from the worker entrypoint
    that subscribes to discord.VoiceClient events. The Discord packet shape
    feeds ``handle_voice_packet``; the VAD lifecycle is driven by
    ``begin_segment`` / ``end_segment`` (or by an injected VAD engine).
    """

    def __init__(
        self,
        *,
        registry_view: NpcRegistryView,
        master_client: MasterIngestionClient,
        stt: SttService,
        seat_lookup: Callable[[str], int | None],
        phase_lookup: Callable[[], tuple[str, str] | None],
        config: VoiceIngestConfig | None = None,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self.registry_view = registry_view
        self.master_client = master_client
        self.stt = stt
        self.seat_lookup = seat_lookup
        # phase_lookup returns (game_id, phase_id) for the current discussion phase, or None.
        self.phase_lookup = phase_lookup
        self.config = config or VoiceIngestConfig()
        self._now_ms = now_ms
        self._open_segments: dict[str, _OpenSegment] = {}
        self.dropped_npc_packets = 0
        self.stt_low_confidence_count = 0
        self.stt_provider_error_count = 0

    # ---------------------------------------------------------- packet boundary

    async def handle_voice_packet(self, *, speaker_user_id: str, pcm: bytes) -> bool:
        """Returns True if the packet was forwarded to VAD, False if dropped."""
        if self.registry_view.is_npc(speaker_user_id):
            self.dropped_npc_packets += 1
            return False
        # Append to whichever segment is open for this speaker (if any).
        seg = self._open_segments.get(speaker_user_id)
        if seg is not None:
            seg.audio_buffer.extend(pcm)
        return True

    # ---------------------------------------------------------- VAD lifecycle

    async def begin_segment(self, *, speaker_user_id: str) -> str | None:
        """Open a VAD window for `speaker_user_id` and notify Master.

        Returns the new `segment_id`, or None if a session is not active
        (no game phase / unknown seat).
        """
        if self.registry_view.is_npc(speaker_user_id):
            return None
        phase = self.phase_lookup()
        if phase is None:
            return None
        game_id, phase_id = phase
        seat_no = self.seat_lookup(speaker_user_id)
        if seat_no is None:
            return None
        segment_id = f"seg_{uuid.uuid4().hex[:12]}"
        now = self._now_ms()
        self._open_segments[speaker_user_id] = _OpenSegment(
            segment_id=segment_id,
            seat_no=seat_no,
            speaker_user_id=speaker_user_id,
            audio_start_ms=now,
        )
        msg = VadSpeechStarted(
            ts=now,
            trace_id=f"vi-{segment_id}",
            game_id=game_id,
            phase_id=phase_id,
            speaker_discord_user_id=speaker_user_id,
            seat_no=seat_no,
            segment_id=segment_id,
            audio_start_ms=now,
        )
        await self.master_client.send_vad_started(msg)
        return segment_id

    async def end_segment(self, *, speaker_user_id: str) -> None:
        seg = self._open_segments.pop(speaker_user_id, None)
        if seg is None:
            return
        phase = self.phase_lookup()
        if phase is None:
            return
        game_id, phase_id = phase
        now = self._now_ms()
        await self.master_client.send_vad_ended(
            VadSpeechEnded(
                ts=now,
                trace_id=f"vi-{seg.segment_id}",
                game_id=game_id,
                phase_id=phase_id,
                speaker_discord_user_id=seg.speaker_user_id,
                seat_no=seg.seat_no,
                segment_id=seg.segment_id,
                audio_end_ms=now,
            )
        )
        await self._run_stt(seg, game_id, phase_id, audio_end_ms=now)

    async def abandon_open_segments(self) -> int:
        """Restart-time cleanup. Returns count of abandoned segments."""
        n = len(self._open_segments)
        self._open_segments.clear()
        if n:
            log.info("voice_ingest_restart abandoned_segments=%d", n)
        return n

    # ---------------------------------------------------------- STT pipeline

    async def _run_stt(
        self,
        seg: _OpenSegment,
        game_id: str,
        phase_id: str,
        *,
        audio_end_ms: int,
    ) -> None:
        try:
            result: SttResult = await self.stt.transcribe(
                audio=bytes(seg.audio_buffer),
                language=self.config.stt_language,
                timeout_s=self.config.stt_timeout_s,
            )
        except SttProviderError as exc:
            self.stt_provider_error_count += 1
            log.info(
                "stt_request_failed game=%s phase=%s segment=%s reason=%s",
                game_id,
                phase_id,
                seg.segment_id,
                exc.failure_reason,
            )
            await self.master_client.send_stt_failed(
                SttFailed(
                    ts=self._now_ms(),
                    trace_id=f"vi-{seg.segment_id}",
                    game_id=game_id,
                    phase_id=phase_id,
                    speaker_discord_user_id=seg.speaker_user_id,
                    seat_no=seg.seat_no,
                    segment_id=seg.segment_id,
                    failure_reason=exc.failure_reason,
                )
            )
            return
        except Exception:
            log.exception(
                "stt_request_failed_unexpected game=%s segment=%s",
                game_id,
                seg.segment_id,
            )
            await self.master_client.send_stt_failed(
                SttFailed(
                    ts=self._now_ms(),
                    trace_id=f"vi-{seg.segment_id}",
                    game_id=game_id,
                    phase_id=phase_id,
                    speaker_discord_user_id=seg.speaker_user_id,
                    seat_no=seg.seat_no,
                    segment_id=seg.segment_id,
                    failure_reason="stt_provider_error",
                )
            )
            return

        if result.confidence < self.config.confidence_threshold:
            self.stt_low_confidence_count += 1
            log.info(
                "stt_low_confidence game=%s segment=%s conf=%.2f",
                game_id,
                seg.segment_id,
                result.confidence,
            )
            await self.master_client.send_stt_failed(
                SttFailed(
                    ts=self._now_ms(),
                    trace_id=f"vi-{seg.segment_id}",
                    game_id=game_id,
                    phase_id=phase_id,
                    speaker_discord_user_id=seg.speaker_user_id,
                    seat_no=seg.seat_no,
                    segment_id=seg.segment_id,
                    failure_reason="stt_low_confidence",
                )
            )
            return

        co_decl = result.co_declaration if result.co_declaration in (
            "seer", "medium", "knight",
        ) else None
        await self.master_client.send_speech_event_payload(
            SpeechEventPayload(
                ts=self._now_ms(),
                trace_id=f"vi-{seg.segment_id}",
                game_id=game_id,
                phase_id=phase_id,
                seat_no=seg.seat_no,
                speaker_discord_user_id=seg.speaker_user_id,
                segment_id=seg.segment_id,
                text=result.text,
                confidence=result.confidence,
                duration_ms=result.duration_ms,
                audio_start_ms=seg.audio_start_ms,
                audio_end_ms=audio_end_ms,
                summary=result.summary,
                co_declaration=co_decl,  # type: ignore[arg-type]
            )
        )

    # ---------------------------------------------------------- heartbeat

    async def heartbeat_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.master_client.send_heartbeat(Heartbeat(ts=self._now_ms(), trace_id="vi-hb"))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.config.heartbeat_interval_s)
            except TimeoutError:
                continue


__all__ = [
    "VadEngine",
    "VoiceIngestConfig",
    "VoiceIngestService",
]


# keep imports referenced
_ = (Awaitable, Callable)
