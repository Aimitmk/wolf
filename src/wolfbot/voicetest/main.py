"""Voice-capture test bot — entrypoint.

Reuses the same ``WolfbotAudioSink`` → ``VoiceIngestService`` path that
Master runs in production, but with:

* STT stubbed (``_NoOpSttService`` returns confidence=1.0 / text="" so
  the ingest service reaches the success branch and the per-segment
  audio dump fires unchanged).
* Master WS client stubbed to in-process logs.
* ``seat_lookup`` / ``phase_lookup`` returning fixed values so the
  ingest service treats every speaker as seat 1 in a synthetic
  ``voicetest::day1::DAY_DISCUSSION::1`` phase.

Segment boundaries come from the same Discord
``on_voice_member_speaking_start`` / ``on_voice_member_speaking_stop``
listeners the production sink uses.

The ``SilenceGeneratorSink`` wrap is toggled by
``VOICETEST_USE_SILENCE_GENERATOR`` so an operator can record the
same speech twice (on / off) and diff the resulting WAVs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Callable, Sequence

import discord
from discord.ext import voice_recv
from dotenv import load_dotenv
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from wolfbot.domain.ws_messages import (
    Heartbeat,
    SpeechEventPayload,
    SttFailed,
    VadSpeechEnded,
    VadSpeechStarted,
)
from wolfbot.master.voice.audio_sink import WolfbotAudioSink
from wolfbot.master.voice.stt_service import RosterEntry, SttResult
from wolfbot.master.voice.voice_ingest_service import (
    VoiceIngestConfig,
    VoiceIngestService,
)

log = logging.getLogger("wolfbot.voicetest")

_FAKE_GAME_ID = "voicetest"
_FAKE_PHASE_ID = "voicetest::day1::DAY_DISCUSSION::1"


class VoicetestSettings(BaseSettings):
    """Voice-test bot configuration.

    Env file path is selected by ``WOLFBOT_VOICETEST_ENV``; default is
    ``.env.voicetest`` in the working directory.
    """

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    VOICETEST_DISCORD_TOKEN: SecretStr
    VOICETEST_GUILD_ID: int
    VOICETEST_VOICE_CHANNEL_ID: int
    WOLFBOT_VOICE_DEBUG_DIR: str
    VOICETEST_USE_SILENCE_GENERATOR: bool = True
    # Drop segments whose buffered audio sounds like silence rather than
    # speech. Discord's speaking-start fires on any audio above a low
    # threshold (breathing, keyboard noise, room hum), so without this
    # gate the dump dir fills with noise-only WAVs.
    # ``MIN_RMS`` is a 16-bit signed RMS threshold computed across the
    # full segment; speech is typically 1000-5000, breathing/typing
    # 100-500, true silence 0-100.
    VOICETEST_MIN_RMS: int = 600
    # ``MIN_DURATION_MS`` rejects micro-bursts (mouse clicks, single
    # syllables triggered by the silence-padding playback). Real
    # utterances are almost always ≥300ms.
    VOICETEST_MIN_DURATION_MS: int = 300
    # When true, run kept segments through the same STT stack the
    # production master uses (selected by ``VOICE_STT_PROVIDER`` -
    # gemini multimodal *or* Groq Whisper + xAI analyzer). When false
    # (default), STT is stubbed and only the WAV is dumped.
    VOICETEST_USE_STT: bool = False
    LOG_LEVEL: str = "INFO"

    # ----- production-equivalent STT credentials -----
    # These are the same keys the master process reads from
    # ``.env.master``. ``_run`` loads ``.env.master`` first and the
    # voicetest env file second (with override), so voicetest
    # transparently reuses whatever STT provider production is set up
    # for - no duplication of API keys in the voicetest env.
    VOICE_STT_PROVIDER: str = "gemini"
    VOICE_LLM_API_KEY: SecretStr | None = None
    VOICE_LLM_MODEL: str = "gemini-2.0-flash-lite"
    GROQ_STT_API_KEY: SecretStr | None = None
    GROQ_STT_MODEL: str = "whisper-large-v3-turbo"
    GROQ_STT_BASE_URL: str = "https://api.groq.com/openai/v1"
    GAMEPLAY_LLM_API_KEY: SecretStr | None = None
    GAMEPLAY_LLM_MODEL: str = "grok-4-1-fast"
    GAMEPLAY_LLM_BASE_URL: str | None = None
    # Pre-STT silence gate, mirrored from MasterSettings so the voice
    # path uses the same threshold as production. ``0`` disables.
    VOICE_PRE_STT_MIN_RMS: int = 0
    VOICE_PRE_STT_MIN_DURATION_MS: int = 0


class _NoNpcRegistryView:
    """Always returns False — the voice-test session has no NPC bots."""

    def is_npc(self, discord_user_id: str) -> bool:
        return False

    def npc_user_ids(self) -> set[str]:
        return set()


class _NoOpMasterClient:
    """Logs each outbound event instead of sending over WS."""

    async def send_vad_started(self, msg: VadSpeechStarted) -> None:
        log.debug(
            "vad_started seat=%s segment=%s ts=%s",
            msg.seat_no, msg.segment_id, msg.ts,
        )

    async def send_vad_ended(self, msg: VadSpeechEnded) -> None:
        log.debug(
            "vad_ended seat=%s segment=%s ts=%s",
            msg.seat_no, msg.segment_id, msg.ts,
        )

    async def send_speech_event_payload(self, msg: SpeechEventPayload) -> None:
        log.info(
            "segment_dumped seat=%s segment=%s window=%s→%sms text=%r",
            msg.seat_no,
            msg.segment_id,
            msg.audio_start_ms,
            msg.audio_end_ms,
            msg.text,
        )

    async def send_stt_failed(self, msg: SttFailed) -> None:
        log.info(
            "stt_failed seat=%s segment=%s reason=%s",
            msg.seat_no, msg.segment_id, msg.failure_reason,
        )

    async def send_heartbeat(self, msg: Heartbeat) -> None:
        return None


class _NoOpSttService:
    """High-confidence empty STT result.

    ``VoiceIngestService._run_stt_inner`` reaches the success branch
    (confidence ≥ 0.6) and triggers the per-segment dump. ``text``
    stays empty so the ``.txt`` sidecar shows ``transcript: <EMPTY>`` —
    the WAV is what matters here, not the transcript.
    """

    async def transcribe(
        self,
        *,
        audio: bytes,
        language: str,
        timeout_s: float,
        roster: Sequence[RosterEntry] | None = None,
    ) -> SttResult:
        del roster  # Voicetest no-op STT has no analyzer to ground.
        bytes_per_sec = 48_000 * 2 * 2  # matches VoiceIngestConfig defaults
        duration_ms = int(len(audio) / bytes_per_sec * 1000) if bytes_per_sec else 0
        return SttResult(
            text="",
            confidence=1.0,
            duration_ms=duration_ms,
        )


def _seat_lookup(_uid: str) -> int | None:
    return 1


def _phase_lookup() -> tuple[str, str] | None:
    return (_FAKE_GAME_ID, _FAKE_PHASE_ID)


def _make_live_vc_roster_lookup(
    bot: discord.Client, voice_channel_id: int
) -> Callable[[], list[tuple[int, str]]]:
    """Build a roster lookup that pulls live VC display names.

    The voicetest bot is in the same voice channel as the human
    speaker plus any NPC bots, so the channel's member list is the
    ground truth for "what names does the speaker actually see /
    hear in this room". Mirrors the production resolver in
    ``main.py`` which falls back to ``Seat.display_name`` only when
    the live member is uncached - operators reported NPC bots
    sometimes appear in VC under a different nickname than the
    persona's stored ``display_name`` (e.g. operator renamed the
    bot in server settings), and the analyzer needs to ground on
    the *spoken* name, not the internal canonical handle.
    """

    def lookup() -> list[tuple[int, str]]:
        ch = bot.get_channel(voice_channel_id)
        if not isinstance(ch, discord.VoiceChannel):
            return []
        # ``ch.members`` is the cached list of guild members
        # currently connected to this voice channel. Sort by id so
        # the seat numbering stays stable across calls.
        members = sorted(ch.members, key=lambda m: m.id)
        roster: list[tuple[int, str]] = []
        for i, member in enumerate(members, start=1):
            roster.append((i, member.display_name))
        return roster

    return lookup


def _build_production_stt(settings: VoicetestSettings):  # type: ignore[no-untyped-def]
    """Construct the same STT instance the production master uses.

    Selected by ``VOICE_STT_PROVIDER`` (gemini / groq) loaded from
    ``.env.master``. Raises ``SystemExit`` with a precise hint when
    the chosen provider's credentials are missing - the operator
    should fix the production env, not duplicate keys here.
    """
    provider = (settings.VOICE_STT_PROVIDER or "gemini").lower()
    if provider == "groq":
        if settings.GROQ_STT_API_KEY is None:
            raise SystemExit(
                "VOICETEST_USE_STT=true with VOICE_STT_PROVIDER=groq "
                "requires GROQ_STT_API_KEY (set in .env.master)."
            )
        if settings.GAMEPLAY_LLM_API_KEY is None:
            raise SystemExit(
                "VOICETEST_USE_STT=true with VOICE_STT_PROVIDER=groq "
                "requires GAMEPLAY_LLM_API_KEY for the analyzer step "
                "(set in .env.master)."
            )
        from wolfbot.master.voice.stt_service import GroqWhisperAudioAnalyzer

        analyzer_base_url = (
            settings.GAMEPLAY_LLM_BASE_URL or "https://api.x.ai/v1"
        )
        log.info(
            "voicetest_stt=ON provider=groq whisper=%s analyzer=%s @ %s",
            settings.GROQ_STT_MODEL,
            settings.GAMEPLAY_LLM_MODEL,
            analyzer_base_url,
        )
        return GroqWhisperAudioAnalyzer(
            groq_api_key=settings.GROQ_STT_API_KEY.get_secret_value(),
            groq_model=settings.GROQ_STT_MODEL,
            groq_base_url=settings.GROQ_STT_BASE_URL,
            analyzer_api_key=settings.GAMEPLAY_LLM_API_KEY.get_secret_value(),
            analyzer_model=settings.GAMEPLAY_LLM_MODEL,
            analyzer_base_url=analyzer_base_url,
        )

    if provider == "gemini":
        if settings.VOICE_LLM_API_KEY is None:
            raise SystemExit(
                "VOICETEST_USE_STT=true with VOICE_STT_PROVIDER=gemini "
                "requires VOICE_LLM_API_KEY (set in .env.master)."
            )
        from wolfbot.master.voice.stt_service import GeminiAudioAnalyzer

        log.info(
            "voicetest_stt=ON provider=gemini model=%s",
            settings.VOICE_LLM_MODEL,
        )
        return GeminiAudioAnalyzer(
            api_key=settings.VOICE_LLM_API_KEY.get_secret_value(),
            model=settings.VOICE_LLM_MODEL,
        )

    raise SystemExit(
        f"Unknown VOICE_STT_PROVIDER={provider!r} - expected 'gemini' or 'groq'"
    )


def _pcm_rms_s16le(pcm: bytes) -> int:
    """Root-mean-square magnitude across all 16-bit signed LE samples.

    Returns 0 for empty / odd-length buffers (treat as silence).
    Uses pure Python so we don't pick up an audioop deprecation
    surface and don't pull numpy into the voicetest dependency
    closure.
    """
    n = len(pcm) // 2
    if n == 0:
        return 0
    import struct

    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    sq = 0
    for s in samples:
        sq += s * s
    return int((sq / n) ** 0.5)


def _install_silence_gate(
    *,
    min_rms: int,
    min_duration_ms: int,
    pcm_bytes_per_ms: int,
) -> None:
    """Wrap ``voice_debug_dump.dump_segment`` with an RMS+duration gate.

    Discord's speaking-start indicator fires on any audio above a low
    threshold (breathing, typing, room noise), and the
    SilenceGeneratorSink keeps the segment "open" by injecting zero
    frames. The result is a steady stream of dumps whose buffer is
    mostly silence with sub-syllable spikes - useless for inspecting
    actual speech recordings.

    ``VoiceIngestService._run_stt_inner`` does
    ``from wolfbot.master.voice.voice_debug_dump import dump_segment``
    *inside the function body*, so a module-level monkey-patch
    applied before any segment fires will be picked up by every
    subsequent call without touching production code.
    """
    import dataclasses
    from datetime import datetime

    import wolfbot.master.voice.voice_debug_dump as _dump_mod

    original = _dump_mod.dump_segment

    def _stamp(audio_start_ms: int) -> str:
        # Local-time wall clock so the operator scanning ``ls`` in the
        # dump dir can see at a glance which utterance came when. ``ms``
        # disambiguates back-to-back segments that fire in the same
        # second, which is common during silence-padded VAD bursts.
        dt = datetime.fromtimestamp(audio_start_ms / 1000.0)
        return dt.strftime("%Y%m%d-%H%M%S-") + f"{audio_start_ms % 1000:03d}"

    async def gated_dump_segment(record, pcm):  # type: ignore[no-untyped-def]
        duration_ms = len(pcm) // pcm_bytes_per_ms if pcm_bytes_per_ms else 0
        rms = _pcm_rms_s16le(pcm)
        if duration_ms < min_duration_ms:
            log.info(
                "voicetest_dump_skip reason=too_short segment=%s duration_ms=%d rms=%d",
                record.segment_id,
                duration_ms,
                rms,
            )
            return
        if rms < min_rms:
            log.info(
                "voicetest_dump_skip reason=below_rms segment=%s rms=%d duration_ms=%d",
                record.segment_id,
                rms,
                duration_ms,
            )
            return
        log.info(
            "voicetest_dump_keep segment=%s rms=%d duration_ms=%d",
            record.segment_id,
            rms,
            duration_ms,
        )
        # Prepend a wall-clock timestamp to ``segment_id`` so the
        # underlying ``dump_segment`` writes ``<YYYYMMDD-HHMMSS-mmm>_<id>.wav``.
        # The sanitizer keeps ``-`` and ``_`` intact, so this passes
        # through unchanged.
        stamped = dataclasses.replace(
            record,
            segment_id=f"{_stamp(record.audio_start_ms)}_{record.segment_id}",
        )
        await original(stamped, pcm)

    _dump_mod.dump_segment = gated_dump_segment


async def _run() -> None:
    # Load production credentials first so STT keys (VOICE_LLM_API_KEY,
    # GROQ_STT_API_KEY, GAMEPLAY_LLM_API_KEY, etc.) come through without
    # duplication. The voicetest-specific file is loaded second with
    # ``override=True`` so any voicetest-only setting wins - including
    # the discord token, which is intentionally a different bot from
    # the master.
    load_dotenv(".env.master")
    env_path = os.environ.get("WOLFBOT_VOICETEST_ENV", ".env.voicetest")
    load_dotenv(env_path, override=True)
    settings = VoicetestSettings()  # type: ignore[call-arg]

    # The dump module reads WOLFBOT_VOICE_DEBUG_DIR off os.environ — make
    # sure it's set even if the operator put it in the .env file rather
    # than the parent shell environment.
    os.environ["WOLFBOT_VOICE_DEBUG_DIR"] = settings.WOLFBOT_VOICE_DEBUG_DIR

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Default intents already include ``voice_states``, which is all we
    # need for ``on_voice_member_speaking_*`` to fire. We deliberately do
    # NOT request the privileged ``members`` intent so this bot can be
    # run without enabling Server Members Intent in the developer
    # portal — the trade-off is that ``member.display_name`` may fall
    # back to the global username (User.name) when the member isn't in
    # the cache, which only affects the dump subdirectory name.
    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)

    ingest_config = VoiceIngestConfig(
        pre_stt_min_rms=settings.VOICE_PRE_STT_MIN_RMS,
        pre_stt_min_duration_ms=settings.VOICE_PRE_STT_MIN_DURATION_MS,
    )
    pcm_bytes_per_ms = (
        ingest_config.pcm_sample_rate
        * ingest_config.pcm_channels
        * ingest_config.pcm_sample_width
        // 1000
    )
    _install_silence_gate(
        min_rms=settings.VOICETEST_MIN_RMS,
        min_duration_ms=settings.VOICETEST_MIN_DURATION_MS,
        pcm_bytes_per_ms=pcm_bytes_per_ms,
    )
    log.info(
        "voicetest_silence_gate min_rms=%d min_duration_ms=%d",
        settings.VOICETEST_MIN_RMS,
        settings.VOICETEST_MIN_DURATION_MS,
    )

    from wolfbot.master.voice.stt_service import SttService

    stt_service: SttService
    if settings.VOICETEST_USE_STT:
        stt_service = _build_production_stt(settings)
    else:
        stt_service = _NoOpSttService()
        log.info("voicetest_stt=OFF (transcript will be empty)")

    voice_ingest = VoiceIngestService(
        registry_view=_NoNpcRegistryView(),
        master_client=_NoOpMasterClient(),
        stt=stt_service,
        seat_lookup=_seat_lookup,
        phase_lookup=_phase_lookup,
        config=ingest_config,
        roster_lookup=_make_live_vc_roster_lookup(
            bot, settings.VOICETEST_VOICE_CHANNEL_ID
        ),
    )

    stop = asyncio.Event()
    vc_ref: list[voice_recv.VoiceRecvClient | None] = [None]

    async def _join_vc() -> None:
        from wolfbot.master.voice.voice_recv_dave_patch import (
            apply_dave_decrypt_patch,
        )
        from wolfbot.master.voice.voice_recv_resilience import (
            apply_packet_router_resilience,
        )

        apply_packet_router_resilience()
        apply_dave_decrypt_patch()

        ch = bot.get_channel(settings.VOICETEST_VOICE_CHANNEL_ID)
        if not isinstance(ch, discord.VoiceChannel):
            log.error(
                "voicetest_channel_not_found id=%s",
                settings.VOICETEST_VOICE_CHANNEL_ID,
            )
            stop.set()
            return
        try:
            vc = await ch.connect(cls=voice_recv.VoiceRecvClient)
        except Exception:
            log.exception("voicetest_join_failed")
            stop.set()
            return

        sink: voice_recv.AudioSink = WolfbotAudioSink(
            voice_ingest, loop=asyncio.get_running_loop()
        )
        if settings.VOICETEST_USE_SILENCE_GENERATOR:
            sink = voice_recv.SilenceGeneratorSink(sink)
            log.info("voicetest_silence_generator=ON (matches production)")
        else:
            log.info("voicetest_silence_generator=OFF (raw real-frames only)")
        vc.listen(sink)
        vc_ref[0] = vc
        log.info(
            "voicetest_joined channel=%s dump_dir=%s",
            settings.VOICETEST_VOICE_CHANNEL_ID,
            settings.WOLFBOT_VOICE_DEBUG_DIR,
        )

        # Snapshot the roster the analyzer will see and log it once
        # so the operator can confirm at a glance which display
        # names will end up in the system prompt for this session.
        roster_now = voice_ingest.roster_lookup() if voice_ingest.roster_lookup else []
        if roster_now:
            log.info(
                "voicetest_roster_snapshot %s",
                ", ".join(f"席{seat}: {name}" for seat, name in roster_now),
            )
        else:
            log.warning(
                "voicetest_roster_empty - analyzer will fall back to "
                "the un-grounded prompt (no VC members visible yet)"
            )

    @bot.event
    async def on_ready() -> None:
        log.info("voicetest_ready user=%s", bot.user)
        await _join_vc()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    bot_task = asyncio.create_task(
        bot.start(settings.VOICETEST_DISCORD_TOKEN.get_secret_value())
    )

    try:
        await stop.wait()
    finally:
        log.info("voicetest_shutting_down")
        vc = vc_ref[0]
        if vc is not None:
            with contextlib.suppress(Exception):
                if vc.is_connected():
                    await vc.disconnect()
        with contextlib.suppress(Exception):
            await bot.close()
        with contextlib.suppress(Exception):
            await bot_task


def run() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    run()
