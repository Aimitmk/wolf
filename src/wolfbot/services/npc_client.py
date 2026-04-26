"""NPC-side master client.

Drives the WS connection from the NPC bot's perspective:

1. Register with Master via `npc_register` and wait for `npc_registered`.
2. Send periodic heartbeats.
3. Receive `logic_packet` (cached by `packet_id`) and `speak_request`.
4. Compose a `SpeakResult` via `NpcSpeechService` and send it back.
5. On `playback_authorized`: synthesize via TTS, call playback, then emit
   `tts_finished` / `tts_failed` and `playback_finished` / `playback_failed`.
6. On `playback_rejected`: drop the queued utterance silently (per spec).

The class exposes `process_message(payload)` so unit tests can drive
inbound traffic deterministically without standing up a WS connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from wolfbot.domain.ws_messages import (
    Heartbeat,
    LogicPacket,
    NpcRegister,
    NpcRegistered,
    PlaybackAuthorized,
    PlaybackFailed,
    PlaybackFinished,
    PlaybackRejected,
    SpeakRequest,
    TtsFailed,
    TtsFinished,
)
from wolfbot.services.npc_speech_service import NpcSpeechService
from wolfbot.services.tts_service import (
    InMemoryTtsCache,
    TtsProviderError,
    TtsRequest,
    TtsService,
)
from wolfbot.services.voice_playback_service import (
    VoicePlayback,
    VoicePlaybackError,
)

log = logging.getLogger(__name__)


@dataclass
class NpcClientConfig:
    npc_id: str
    discord_bot_user_id: str
    voice_id: str
    supported_voices: tuple[str, ...] = ()
    version: str = "0.0.1"


@dataclass
class _AuthorizedPlayback:
    request_id: str
    text: str


@dataclass
class _PendingForPlayback:
    """Tracks the SpeakResult we sent so we can find its text on authorization."""

    text: str
    voice_id: str


@dataclass
class NpcClient:
    config: NpcClientConfig
    speech: NpcSpeechService
    tts: TtsService
    playback: VoicePlayback
    send: Callable[[str], Awaitable[None]]
    now_ms: Callable[[], int]
    cache: InMemoryTtsCache = field(default_factory=lambda: InMemoryTtsCache(max_entries=64))

    _logic_cache: dict[str, LogicPacket] = field(default_factory=dict)
    _pending_playback: dict[str, _PendingForPlayback] = field(default_factory=dict)
    pending_authorizations: list[_AuthorizedPlayback] = field(default_factory=list)
    registered: bool = False

    # ---------------------------------------------------------- registration

    async def register(self, trace_id: str = "register") -> None:
        msg = NpcRegister(
            ts=self.now_ms(),
            trace_id=trace_id,
            npc_id=self.config.npc_id,
            discord_bot_user_id=self.config.discord_bot_user_id,
            supported_voices=self.config.supported_voices,
            version=self.config.version,
        )
        await self.send(msg.model_dump_json())

    async def heartbeat(self) -> None:
        await self.send(
            Heartbeat(ts=self.now_ms(), trace_id="hb", npc_id=self.config.npc_id).model_dump_json()
        )

    # ---------------------------------------------------------- inbound

    async def process_message(self, raw_json: str) -> None:
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            log.warning("npc_client_invalid_json")
            return
        if not isinstance(payload, dict) or "type" not in payload:
            log.warning("npc_client_missing_type")
            return
        t = payload["type"]
        if t == "npc_registered":
            self._on_registered(NpcRegistered.model_validate(payload))
        elif t == "logic_packet":
            self._on_logic_packet(LogicPacket.model_validate(payload))
        elif t == "speak_request":
            await self._on_speak_request(SpeakRequest.model_validate(payload))
        elif t == "playback_authorized":
            await self._on_playback_authorized(PlaybackAuthorized.model_validate(payload))
        elif t == "playback_rejected":
            self._on_playback_rejected(PlaybackRejected.model_validate(payload))
        else:
            log.info("npc_client_unhandled_type type=%s", t)

    def _on_registered(self, msg: NpcRegistered) -> None:
        self.registered = True
        log.info(
            "npc_registered npc_id=%s seat=%s game=%s",
            msg.npc_id,
            msg.assigned_seat,
            msg.game_id,
        )

    def _on_logic_packet(self, packet: LogicPacket) -> None:
        self._logic_cache[packet.packet_id] = packet

    async def _on_speak_request(self, request: SpeakRequest) -> None:
        logic = self._logic_cache.get(request.logic_packet_id)
        if logic is None:
            # No matching LogicPacket — best-effort generate without context.
            log.warning("npc_speak_request_missing_logic packet=%s", request.logic_packet_id)
            logic = LogicPacket(
                ts=self.now_ms(),
                trace_id=request.trace_id,
                packet_id=request.logic_packet_id,
                phase_id=request.phase_id,
                recipient_npc_id=request.npc_id,
                public_state_summary="",
                logic_candidates=(),
                pressure={},
                expires_at_ms=request.expires_at_ms,
            )
        result = await self.speech.respond(logic=logic, request=request, now_ms=self.now_ms())
        if result.status == "accepted" and result.text is not None:
            self._pending_playback[result.request_id] = _PendingForPlayback(
                text=result.text, voice_id=self.config.voice_id
            )
        await self.send(result.model_dump_json())

    async def _on_playback_authorized(self, auth: PlaybackAuthorized) -> None:
        pending = self._pending_playback.pop(auth.request_id, None)
        if pending is None:
            log.warning(
                "npc_playback_authorized_unknown request=%s",
                auth.request_id,
            )
            return
        self.pending_authorizations.append(
            _AuthorizedPlayback(request_id=auth.request_id, text=pending.text)
        )
        # Synthesize.
        req = TtsRequest(text=pending.text, voice_id=pending.voice_id)
        cached = self.cache.get(req)
        try:
            if cached is not None:
                tts_result = cached
                tts_duration_ms = cached.duration_ms
            else:
                tts_result = await self.tts.synthesize(req)
                self.cache.put(req, tts_result)
                tts_duration_ms = tts_result.duration_ms
        except TtsProviderError as exc:
            await self.send(
                TtsFailed(
                    ts=self.now_ms(),
                    trace_id=auth.trace_id,
                    request_id=auth.request_id,
                    npc_id=auth.npc_id,
                    failure_reason=exc.failure_reason,
                ).model_dump_json()
            )
            return
        await self.send(
            TtsFinished(
                ts=self.now_ms(),
                trace_id=auth.trace_id,
                request_id=auth.request_id,
                npc_id=auth.npc_id,
                tts_duration_ms=tts_duration_ms,
                audio_size_bytes=len(tts_result.audio),
            ).model_dump_json()
        )
        # Playback (gated — never plays without authorization).
        try:
            started, finished = await self.playback.play(
                audio=tts_result.audio, sample_rate=tts_result.sample_rate
            )
        except VoicePlaybackError as exc:
            await self.send(
                PlaybackFailed(
                    ts=self.now_ms(),
                    trace_id=auth.trace_id,
                    request_id=auth.request_id,
                    npc_id=auth.npc_id,
                    failure_reason=exc.failure_reason,
                ).model_dump_json()
            )
            return
        except Exception:
            log.exception("npc_playback_unexpected_error request=%s", auth.request_id)
            await self.send(
                PlaybackFailed(
                    ts=self.now_ms(),
                    trace_id=auth.trace_id,
                    request_id=auth.request_id,
                    npc_id=auth.npc_id,
                    failure_reason="discord_playback_error",
                ).model_dump_json()
            )
            return
        await self.send(
            PlaybackFinished(
                ts=self.now_ms(),
                trace_id=auth.trace_id,
                request_id=auth.request_id,
                npc_id=auth.npc_id,
                started_at_ms=started,
                finished_at_ms=finished,
            ).model_dump_json()
        )

    def _on_playback_rejected(self, msg: PlaybackRejected) -> None:
        # Drop the pending playback silently — no audio plays per spec.
        self._pending_playback.pop(msg.request_id, None)
        log.info(
            "npc_playback_rejected request=%s reason=%s",
            msg.request_id,
            msg.failure_reason,
        )


__all__ = ["NpcClient", "NpcClientConfig"]


# Force imports referenced for typing extensions.
_ = (asyncio,)
