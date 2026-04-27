"""NPC bot worker entrypoint.

Reads ``NPC_*`` env vars, connects to Discord VC, opens a WS connection
to Master, registers, and runs the heartbeat + message loop.  On
``speak_request`` the NPC generates text via Grok, synthesizes via
VOICEVOX, and plays the audio into the voice channel.

Run with::

    uv run wolfbot-npc

(after exporting the env vars below — ``NPC_ID``, ``NPC_DISCORD_TOKEN``,
``MASTER_WS_URL``, ``MASTER_NPC_PSK``, ``XAI_API_KEY``, ``TTS_VOICE_ID``,
``VOICEVOX_URL``, ``MAIN_VOICE_CHANNEL_ID``.)
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import time

import discord

log = logging.getLogger(__name__)


def _read_env(name: str, *, required: bool = True, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise SystemExit(f"missing required env var {name}")
    return val or ""


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _main() -> None:
    npc_id = _read_env("NPC_ID")
    discord_token = _read_env("NPC_DISCORD_TOKEN")
    master_ws_url = _read_env("MASTER_WS_URL")
    psk = _read_env("MASTER_NPC_PSK")
    xai_api_key = _read_env("XAI_API_KEY")
    xai_model = _read_env("XAI_MODEL", required=False, default="grok-4-1-fast")
    voice_id = _read_env("TTS_VOICE_ID", required=False, default="3")
    voicevox_url = _read_env(
        "VOICEVOX_URL", required=False, default="http://localhost:50021")
    vc_channel_id = int(_read_env("MAIN_VOICE_CHANNEL_ID"))
    guild_id = int(_read_env("DISCORD_GUILD_ID"))
    heartbeat_interval = float(
        _read_env("HEARTBEAT_INTERVAL_S", required=False, default="5"))
    log_level = _read_env("LOG_LEVEL", required=False, default="INFO")

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    log.info(
        "npc_bot_starting npc_id=%s ws=%s voice_id=%s voicevox=%s",
        npc_id, master_ws_url, voice_id, voicevox_url,
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
        guild = bot.get_guild(guild_id)
        if guild is None:
            log.error("npc_guild_not_found id=%s", guild_id)
            return
        vc_channel = guild.get_channel(vc_channel_id)
        if vc_channel is None or not isinstance(vc_channel, discord.VoiceChannel):
            log.error("npc_vc_channel_not_found id=%s", vc_channel_id)
            return
        try:
            vc_client_ref[0] = await vc_channel.connect()
            log.info("npc_vc_joined channel=%s", vc_channel_id)
        except Exception:
            log.exception("npc_vc_join_failed channel=%s", vc_channel_id)
        ready_event.set()

    # Start Discord in background
    discord_task = asyncio.create_task(bot.start(discord_token))

    # Wait for VC connection
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=30.0)
    except TimeoutError:
        log.error("npc_discord_ready_timeout")
        raise SystemExit(1) from None

    discord_user_id = str(bot.user.id) if bot.user else "unknown"

    # ---- Build NPC pipeline ----
    from wolfbot.services.npc_client import NpcClient, NpcClientConfig
    from wolfbot.services.npc_generator_grok import GrokNpcGenerator, GrokNpcGeneratorConfig
    from wolfbot.services.npc_speech_service import NpcSpeechService
    from wolfbot.services.tts_service import VoicevoxTtsService
    from wolfbot.services.voice_playback_service import DiscordVoicePlayback

    generator = GrokNpcGenerator(
        api_key=xai_api_key,
        config=GrokNpcGeneratorConfig(model=xai_model),
    )
    speech_service = NpcSpeechService(generator=generator)
    tts = VoicevoxTtsService(base_url=voicevox_url,
                             default_speaker=int(voice_id))

    # ---- Playback function: WAV → discord.VoiceClient.play ----
    async def _play_audio(audio: bytes, sample_rate: int) -> tuple[int, int]:
        vc = vc_client_ref[0]
        if vc is None or not vc.is_connected():
            from wolfbot.services.voice_playback_service import VoicePlaybackError
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
            from wolfbot.services.voice_playback_service import VoicePlaybackError
            raise VoicePlaybackError(f"playback_error: {play_error[0]}")
        return (started, finished)

    playback = DiscordVoicePlayback(play_fn=_play_audio)

    # ---- WS connection to Master ----
    import websockets

    sep = "?" if "?" not in master_ws_url else "&"
    ws_url = f"{master_ws_url}{sep}role=npc&psk={psk}"
    ws = await websockets.connect(ws_url)

    async def _ws_send(msg: str) -> None:
        await ws.send(msg)

    client = NpcClient(
        config=NpcClientConfig(
            npc_id=npc_id,
            discord_bot_user_id=discord_user_id,
            voice_id=voice_id,
        ),
        speech=speech_service,
        tts=tts,
        playback=playback,
        send=_ws_send,
        now_ms=_now_ms,
    )

    # Register with Master
    await client.register()
    log.info("npc_registered npc_id=%s user_id=%s", npc_id, discord_user_id)

    # ---- Background tasks ----
    stop = asyncio.Event()

    async def _heartbeat_loop() -> None:
        while not stop.is_set():
            try:
                await client.heartbeat()
            except Exception:
                log.exception("npc_heartbeat_failed")
            await asyncio.sleep(heartbeat_interval)

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

    log.info("npc_bot_running npc_id=%s", npc_id)

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

    log.info("npc_bot_stopped npc_id=%s", npc_id)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["main"]
