"""Bridge between discord-ext-voice-recv and VoiceIngestService.

``WolfbotAudioSink`` is an :class:`voice_recv.AudioSink` subclass that:

* Feeds decoded PCM frames into :pymethod:`VoiceIngestService.handle_voice_packet`.
* Uses the library's synthetic ``on_voice_member_speaking_start`` /
  ``on_voice_member_speaking_stop`` events as a VAD signal to drive
  ``begin_segment`` / ``end_segment``.

All sink callbacks (``write``, ``on_voice_member_*``) are invoked from an
internal reader thread, so async work is scheduled on the bot's event loop
via :func:`asyncio.run_coroutine_threadsafe`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import voice_recv

if TYPE_CHECKING:
    from wolfbot.services.voice_ingest_service import VoiceIngestService

log = logging.getLogger(__name__)


class WolfbotAudioSink(voice_recv.AudioSink):  # type: ignore[misc]
    """Receives per-user PCM and routes it to VoiceIngestService."""

    def __init__(
        self,
        voice_ingest: VoiceIngestService,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        super().__init__()
        self._ingest = voice_ingest
        self._loop = loop

    # ---- core sink interface ----

    def wants_opus(self) -> bool:
        return False

    def write(self, user: discord.Member | discord.User | None, data: voice_recv.VoiceData) -> None:
        if user is None or data.pcm is None:
            return
        uid = str(user.id)
        asyncio.run_coroutine_threadsafe(
            self._ingest.handle_voice_packet(
                speaker_user_id=uid, pcm=data.pcm),
            self._loop,
        )

    def cleanup(self) -> None:
        pass

    # ---- VAD via speaking indicators ----

    @voice_recv.AudioSink.listener()  # type: ignore[misc]
    def on_voice_member_speaking_start(self, member: discord.Member) -> None:
        uid = str(member.id)
        asyncio.run_coroutine_threadsafe(
            self._ingest.begin_segment(speaker_user_id=uid),
            self._loop,
        )

    @voice_recv.AudioSink.listener()  # type: ignore[misc]
    def on_voice_member_speaking_stop(self, member: discord.Member) -> None:
        uid = str(member.id)
        asyncio.run_coroutine_threadsafe(
            self._ingest.end_segment(speaker_user_id=uid),
            self._loop,
        )


__all__ = ["WolfbotAudioSink"]
