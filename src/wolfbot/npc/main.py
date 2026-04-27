"""NPC bot worker entrypoint.

Loads :class:`wolfbot.npc.config.NpcSettings` from the env file pointed
to by ``WOLFBOT_NPC_ENV`` (default ``.env.npc``), connects to Discord
VC, opens a WS connection to Master, registers, and runs the heartbeat
+ message loop. On ``speak_request`` the NPC generates text via the
configured NPC LLM, synthesizes via VOICEVOX, and plays the audio into
the voice channel.

Run with::

    WOLFBOT_NPC_ENV=envs/npc/.env.<persona> uv run wolfbot-npc

Per-persona templates are committed under ``envs/npc/.env.<persona>.example``;
see :file:`envs/npc/README.md` for the setup workflow and persona table.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import time

import discord
from dotenv import load_dotenv

from wolfbot.npc.config import NpcSettings

log = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _main() -> None:
    # Per-persona env files live under `envs/npc/.env.<persona>` (templates
    # are committed at `envs/npc/.env.<persona>.example`; see
    # `envs/npc/README.md`).  The launcher must point WOLFBOT_NPC_ENV at the
    # right file per process — e.g. `WOLFBOT_NPC_ENV=envs/npc/.env.setsu`.
    # `.env.npc` is the legacy fallback when the env var is unset.
    env_path = os.environ.get("WOLFBOT_NPC_ENV", ".env.npc")
    load_dotenv(env_path)
    settings = NpcSettings()  # type: ignore[call-arg]

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Validate the persona key against the canonical NPC pool — fail loud at
    # startup rather than silently fall back to a default at speak time.
    from wolfbot.npc.personas import NPC_PERSONAS_BY_KEY

    if settings.NPC_PERSONA_KEY not in NPC_PERSONAS_BY_KEY:
        valid = ", ".join(sorted(NPC_PERSONAS_BY_KEY.keys()))
        raise SystemExit(
            f"NPC_PERSONA_KEY={settings.NPC_PERSONA_KEY!r} is not a known persona. "
            f"Valid keys: {valid}"
        )

    log.info(
        "npc_bot_starting npc_id=%s persona=%s ws=%s voice_id=%s voicevox=%s",
        settings.NPC_ID,
        settings.NPC_PERSONA_KEY,
        settings.MASTER_WS_URL,
        settings.TTS_VOICE_ID,
        settings.VOICEVOX_URL,
    )

    # ---- Discord client (voice only, no message_content) ----
    intents = discord.Intents.default()
    intents.voice_states = True
    bot = discord.Client(intents=intents)

    vc_client_ref: list[discord.VoiceClient | None] = [None]
    ready_event = asyncio.Event()

    @bot.event
    async def on_ready() -> None:
        log.info("npc_discord_ready user=%s", bot.user)
        guild = bot.get_guild(settings.DISCORD_GUILD_ID)
        if guild is None:
            log.error("npc_guild_not_found id=%s", settings.DISCORD_GUILD_ID)
            return
        vc_channel = guild.get_channel(settings.MAIN_VOICE_CHANNEL_ID)
        if vc_channel is None or not isinstance(vc_channel, discord.VoiceChannel):
            log.error("npc_vc_channel_not_found id=%s",
                      settings.MAIN_VOICE_CHANNEL_ID)
            return
        try:
            vc_client_ref[0] = await vc_channel.connect()
            log.info("npc_vc_joined channel=%s", settings.MAIN_VOICE_CHANNEL_ID)
        except Exception:
            log.exception("npc_vc_join_failed channel=%s",
                          settings.MAIN_VOICE_CHANNEL_ID)
        ready_event.set()

    # Start Discord in background
    discord_task = asyncio.create_task(
        bot.start(settings.NPC_DISCORD_TOKEN.get_secret_value()))

    # Wait for VC connection
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=30.0)
    except TimeoutError:
        log.error("npc_discord_ready_timeout")
        raise SystemExit(1) from None

    discord_user_id = str(bot.user.id) if bot.user else "unknown"

    # ---- Build NPC pipeline ----
    from wolfbot.npc.client import NpcClient, NpcClientConfig
    from wolfbot.npc.generator_factory import make_npc_generator
    from wolfbot.npc.playback import DiscordVoicePlayback, VoicePlaybackError
    from wolfbot.npc.speech_service import NpcSpeechService
    from wolfbot.npc.tts import VoicevoxTtsService

    generator = make_npc_generator(
        settings.npc_decider_config(),
        persona_key=settings.NPC_PERSONA_KEY,
    )
    speech_service = NpcSpeechService(generator=generator)
    tts = VoicevoxTtsService(
        base_url=settings.VOICEVOX_URL,
        default_speaker=int(settings.TTS_VOICE_ID),
    )

    # ---- Playback function: WAV → discord.VoiceClient.play ----
    async def _play_audio(audio: bytes, sample_rate: int) -> tuple[int, int]:
        vc = vc_client_ref[0]
        if vc is None or not vc.is_connected():
            raise VoicePlaybackError("vc_not_connected")

        # Convert raw WAV (possibly 24kHz) to PCM source
        started = _now_ms()
        done_event = asyncio.Event()
        play_error: list[Exception | None] = [None]

        def _after(error: Exception | None) -> None:
            play_error[0] = error
            # Schedule set on the event loop since this callback is from a thread
            bot.loop.call_soon_threadsafe(done_event.set)

        source = discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True)
        vc.play(source, after=_after)
        await done_event.wait()

        finished = _now_ms()
        if play_error[0] is not None:
            raise VoicePlaybackError(f"playback_error: {play_error[0]}")
        return (started, finished)

    playback = DiscordVoicePlayback(play_fn=_play_audio)

    # ---- WS connection to Master ----
    import websockets

    base_url = settings.MASTER_WS_URL
    sep = "?" if "?" not in base_url else "&"
    ws_url = (
        f"{base_url}{sep}role=npc"
        f"&psk={settings.MASTER_NPC_PSK.get_secret_value()}"
    )
    ws = await websockets.connect(ws_url)

    async def _ws_send(msg: str) -> None:
        await ws.send(msg)

    client = NpcClient(
        config=NpcClientConfig(
            npc_id=settings.NPC_ID,
            discord_bot_user_id=discord_user_id,
            persona_key=settings.NPC_PERSONA_KEY,
            voice_id=settings.TTS_VOICE_ID,
        ),
        speech=speech_service,
        tts=tts,
        playback=playback,
        send=_ws_send,
        now_ms=_now_ms,
    )

    # Register with Master
    await client.register()
    log.info("npc_registered npc_id=%s user_id=%s",
             settings.NPC_ID, discord_user_id)

    # ---- Background tasks ----
    stop = asyncio.Event()

    async def _heartbeat_loop() -> None:
        while not stop.is_set():
            try:
                await client.heartbeat()
            except Exception:
                log.exception("npc_heartbeat_failed")
            await asyncio.sleep(settings.HEARTBEAT_INTERVAL_S)

    async def _message_loop() -> None:
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                await client.process_message(raw)
        except websockets.exceptions.ConnectionClosed:
            log.warning("npc_ws_closed")
        except Exception:
            log.exception("npc_message_loop_error")
        finally:
            stop.set()

    hb_task = asyncio.create_task(_heartbeat_loop())
    msg_task = asyncio.create_task(_message_loop())

    log.info("npc_bot_running npc_id=%s", settings.NPC_ID)

    # Wait until the message loop or discord dies
    _done, _pending = await asyncio.wait(
        [discord_task, msg_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    stop.set()
    hb_task.cancel()

    # Cleanup
    with contextlib.suppress(Exception):
        await ws.close()
    vc = vc_client_ref[0]
    if vc is not None and vc.is_connected():
        await vc.disconnect()
    with contextlib.suppress(Exception):
        await bot.close()

    log.info("npc_bot_stopped npc_id=%s", settings.NPC_ID)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["main"]
