"""Master-side TTS playback — Levi narration via VOICEVOX in VC.

NPC bots have their own per-process TTS pipeline
(:mod:`wolfbot.npc.audio.tts` + :mod:`wolfbot.npc.audio.playback`). Master needs a
parallel-but-simpler pipeline: synthesize narration text via the same
VOICEVOX HTTP engine, then push the audio through Master's own
`discord.VoiceClient.play(...)` so phase-transition announcements are
heard in the same voice channel the NPCs play in.

This module is the seam:

  1. ``MasterTtsPlayback.speak(text)`` synthesizes via VOICEVOX, then
     plays the audio through the supplied `discord.VoiceClient`.
  2. ``async with playback.suppress_npc_dispatch(arbiter)`` parks the
     SpeakArbiter's serial-speech gate so an NPC doesn't try to talk
     over Master mid-narration. Implemented by stuffing a sentinel
     request id into ``arbiter._active_playback`` and clearing it when
     the with-block exits — no new arbiter API surface needed.
  3. The per-Master speak operations are mutex-serialized so back-to-
     back announcements (e.g. EXECUTION followed by PHASE_CHANGE within
     a few ms) play in order rather than overlapping at the audio
     mixer.

Deliberately NOT wired through the WS protocol: Master runs in the
same process as the arbiter, so we can call into its private state
without going through ``LogicPacket`` / ``SpeakRequest``. NPCs are
external processes; that's why their pipeline goes via WS.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from wolfbot.npc.audio.tts import (
    InMemoryTtsCache,
    TtsProviderError,
    TtsRequest,
    TtsService,
)

log = logging.getLogger(__name__)


class MasterTtsPlayback:
    """Synthesize + play Master's Levi narration into VC.

    Holds a back-reference to a ``vc_ref`` list so the live VoiceClient
    is read on every speak (the connection may flap across game
    boundaries — see :mod:`wolfbot.main` lifecycle helpers).
    """

    _SENTINEL_PREFIX = "master-narration-"

    def __init__(
        self,
        *,
        tts: TtsService,
        voice_id: str,
        vc_ref: list[Any],
        cache: InMemoryTtsCache | None = None,
    ) -> None:
        self._tts = tts
        self._voice_id = voice_id
        self._vc_ref = vc_ref
        self._cache = cache or InMemoryTtsCache(max_entries=64)
        self._lock = asyncio.Lock()

    @property
    def voice_id(self) -> str:
        return self._voice_id

    async def speak(self, text: str) -> bool:
        """Synthesize ``text`` and play it. Returns False on any failure
        (TTS error, no VC client, playback error). Failures are logged
        but never raise — narration must not crash the engine."""
        text = text.strip()
        if not text:
            return False
        async with self._lock:
            req = TtsRequest(text=text, voice_id=self._voice_id)
            cached = self._cache.get(req)
            try:
                if cached is not None:
                    result = cached
                else:
                    result = await self._tts.synthesize(req)
                    self._cache.put(req, result)
            except TtsProviderError as exc:
                log.warning(
                    "master_tts_synth_failed reason=%s text=%r",
                    exc.failure_reason,
                    text[:80],
                )
                return False
            except Exception:
                log.exception(
                    "master_tts_synth_unexpected text=%r", text[:80]
                )
                return False
            vc = self._vc_ref[0]
            if vc is None or not vc.is_connected():
                log.info("master_tts_skipped reason=vc_not_connected")
                return False
            return await self._play(vc, result.audio)

    async def _play(self, vc: Any, audio: bytes) -> bool:
        # Lazy import keeps test environments without discord.py able
        # to import this module for narration unit tests.
        import discord

        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        play_error: list[Exception | None] = [None]

        def _after(error: Exception | None) -> None:
            play_error[0] = error
            loop.call_soon_threadsafe(done.set)

        try:
            # Some versions of discord.VoiceClient reject `play` while a
            # previous source is still active. Wait briefly for any prior
            # source to finish, but don't block the engine if a prior
            # play stalls — release the lock and let narration drop.
            for _ in range(50):
                if not vc.is_playing():
                    break
                await asyncio.sleep(0.05)
            if vc.is_playing():
                log.info("master_tts_skipped reason=vc_busy")
                return False

            source = discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True)
            vc.play(source, after=_after)
        except Exception:
            log.exception("master_tts_play_failed")
            return False
        try:
            await asyncio.wait_for(done.wait(), timeout=30.0)
        except TimeoutError:
            log.warning("master_tts_play_timeout")
            return False
        if play_error[0] is not None:
            log.warning("master_tts_play_after_error err=%s", play_error[0])
            return False
        return True

    @contextlib.asynccontextmanager
    async def suppress_npc_dispatch(
        self, arbiter: Any | None
    ) -> AsyncIterator[None]:
        """Hold the arbiter's serial-speech gate while Master speaks.

        Mutates ``arbiter._active_playback`` directly. We avoid the
        public dispatch_request API because Master narration is not a
        SpeakResult — there's no NPC bot to authorize, and we don't
        want a DB row in `npc_playback_events` for a Master utterance.

        Before adding the sentinel we wait for any in-flight NPC
        playback to drain. Otherwise an already-authorized NPC
        utterance would play simultaneously with Master narration in
        the same VC. The wait is bounded so a stuck NPC playback
        (timed-out tts_finished/playback_finished) eventually unblocks
        narration via the arbiter's normal expiry sweep.
        """
        if arbiter is not None:
            await self._wait_for_npc_playback_drain(arbiter)
        sentinel: str | None = None
        if arbiter is not None:
            sentinel = f"{self._SENTINEL_PREFIX}{uuid.uuid4().hex[:8]}"
            try:
                arbiter._active_playback.add(sentinel)
            except AttributeError:
                # Defensive: if the arbiter shape ever changes, fall
                # back to no-op. Better to risk audio overlap than to
                # break narration entirely.
                sentinel = None
        try:
            yield
        finally:
            if sentinel is not None and arbiter is not None:
                with contextlib.suppress(AttributeError):
                    arbiter._active_playback.discard(sentinel)

    async def _wait_for_npc_playback_drain(
        self,
        arbiter: Any,
        *,
        max_wait_s: float = 15.0,
        poll_interval_s: float = 0.1,
    ) -> None:
        """Block until the arbiter's `_active_playback` carries no
        non-master entries, or `max_wait_s` elapses.

        Master sentinels (prefixed with ``master-narration-``) are
        ignored so concurrent narrations don't deadlock — they're
        already serialized by `self._lock`."""
        active: set[str] | None = None
        try:
            active = arbiter._active_playback
        except AttributeError:
            return
        if active is None:
            return
        deadline_loops = max(1, int(max_wait_s / poll_interval_s))
        for _ in range(deadline_loops):
            others = [
                rid for rid in active if not rid.startswith(self._SENTINEL_PREFIX)
            ]
            if not others:
                return
            await asyncio.sleep(poll_interval_s)
        log.info(
            "master_tts_drain_timeout pending=%d", len(others)
        )


__all__ = ["MasterTtsPlayback"]
