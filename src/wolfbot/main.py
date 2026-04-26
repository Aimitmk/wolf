"""Entrypoint. Load env → migrate DB → connect Discord → recover → run."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Any

import discord
from discord.ext import commands
from dotenv import load_dotenv

from wolfbot.config import Settings
from wolfbot.persistence.schema import migrate
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discord_service import DiscordBotAdapter, WolfCog
from wolfbot.services.discussion_service import DiscussionService, SqliteSpeechEventStore
from wolfbot.services.game_service import GameService
from wolfbot.services.llm_service import LLMAdapter, make_xai_decider
from wolfbot.services.recovery_service import RecoveryService
from wolfbot.services.timer_service import EngineRegistry

log = logging.getLogger("wolfbot")


async def _run() -> None:
    load_dotenv()
    settings = Settings()  # type: ignore[call-arg]
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    await migrate(settings.WOLFBOT_DB_PATH)
    repo = SqliteRepo(settings.WOLFBOT_DB_PATH)
    await repo.connect()

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    registry = EngineRegistry()
    discord_adapter = DiscordBotAdapter(bot=bot, repo=repo, settings=settings)

    speech_store = SqliteSpeechEventStore(repo._db)

    class _DiscussionPoster:
        """Adapts DiscordBotAdapter.post_public(Game, ...) to the
        SpeechMessagePoster protocol which takes a bare game_id string.
        Loads the Game from the repo so the adapter gets the channel id
        it needs.
        """

        async def post_public(self, game_id: str, text: str, kind: str) -> None:
            game = await repo.load_game(game_id)
            if game is None:
                return
            await discord_adapter.post_public(game, text, kind)

    discussion_service = DiscussionService(
        store=speech_store,
        log_sink=repo,
        message_poster=_DiscussionPoster(),
    )

    decider = make_xai_decider(
        api_key=settings.XAI_API_KEY.get_secret_value(),
        model=settings.XAI_MODEL,
    )
    llm_adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=discord_adapter,
        discussion_service=discussion_service,
    )
    # The reactive_phase_enter callback is set below once the arbiter exists.
    _reactive_phase_cb: list[Any] = []

    async def _on_reactive_phase_enter(game_id: str) -> None:
        if _reactive_phase_cb:
            await _reactive_phase_cb[0].try_dispatch_next(game_id)

    game_service = GameService(
        repo=repo,
        discord=discord_adapter,
        llm=llm_adapter,
        wake=registry,
        on_reactive_phase_enter=_on_reactive_phase_enter,
    )
    discord_adapter.set_game_service(game_service)
    llm_adapter.set_game_service(game_service)

    async def _on_speech_recorded(game_id: str) -> None:
        """After a text SpeechEvent is recorded, try dispatching an NPC reply."""
        if _reactive_phase_cb:
            await _reactive_phase_cb[0].try_dispatch_next(game_id)

    cog = WolfCog(
        bot=bot,
        repo=repo,
        game_service=game_service,
        discord_adapter=discord_adapter,
        llm_adapter=llm_adapter,
        registry=registry,
        settings=settings,
        discussion_service=discussion_service,
        on_speech_recorded=_on_speech_recorded,
    )
    await bot.add_cog(cog)
    bot.tree.add_command(cog.wolf, guild=discord.Object(id=settings.DISCORD_GUILD_ID))

    recovery = RecoveryService(
        repo=repo,
        game_service=game_service,
        registry=registry,
        discord=discord_adapter,
    )
    recovery_done = asyncio.Event()

    # ---- reactive_voice pipeline (Master WS + NPC registry + arbiter) ----
    # Constructed when MASTER_NPC_PSK is set, which signals that the operator
    # wants the WS transport running. Without a PSK the pipeline stays off and
    # reactive_voice games simply rely on the deadline gate (no NPC speech).
    ws_server: Any = None
    if settings.MASTER_NPC_PSK is not None:
        from wolfbot.services.master_ingest_service import MasterIngestService
        from wolfbot.services.master_ws_server import (
            MasterHandlers,
            WebsocketsMasterWsServer,
        )
        from wolfbot.services.npc_registry import InMemoryNpcRegistry
        from wolfbot.services.speak_arbiter import SpeakArbiter

        npc_registry = InMemoryNpcRegistry()

        arbiter = SpeakArbiter(
            repo=repo,
            registry=npc_registry,
            discussion=discussion_service,
        )
        _reactive_phase_cb.append(arbiter)
        recovery._reactive_voice_sweep = arbiter.reactive_voice_recovery_sweep

        class _RepoPhase:
            """Adapts SqliteRepo to MasterIngestService.PhaseLookup."""

            async def get_phase(self, game_id: str) -> tuple[Any, int] | None:
                g = await repo.load_game(game_id)
                if g is None or g.ended_at is not None:
                    return None
                return (g.phase, g.day_number)

            async def get_alive_seat_nos(self, game_id: str) -> list[int]:
                players = await repo.load_players(game_id)
                return sorted(p.seat_no for p in players if p.alive)

        ingest_service = MasterIngestService(
            registry=npc_registry,
            discussion=discussion_service,
            phase_lookup=_RepoPhase(),
        )

        async def _on_speak_result(msg: Any, _ctx: Any) -> None:
            g = await repo.load_game(msg.game_id) if hasattr(msg, "game_id") else None
            # Resolve game from the pending request inside the arbiter
            from wolfbot.domain.discussion import make_phase_id as _mkpi

            pending = arbiter._pending.get(msg.request_id)
            if pending is None:
                return
            g = await repo.load_game(pending.game_id)
            if g is None or g.ended_at is not None:
                return
            accepted, _reason = await arbiter.handle_speak_result(
                msg,
                current_phase_id=_mkpi(g.id, g.day_number, g.phase),
                day=g.day_number,
                phase=g.phase,
            )
            if accepted:
                log.info("speak_result_accepted npc=%s game=%s", msg.npc_id, g.id)

        async def _on_tts_finished(msg: Any, _ctx: Any) -> None:
            await arbiter.handle_tts_finished(msg)

        async def _on_tts_failed(msg: Any, _ctx: Any) -> None:
            await arbiter.handle_tts_failed(msg)
            # Gate cleared — try dispatching next NPC.
            pending = arbiter._pending.get(msg.request_id)
            if pending is not None:
                await arbiter.try_dispatch_next(pending.game_id)

        async def _on_playback_finished(msg: Any, _ctx: Any) -> None:
            # Resolve game_id before the pending entry is popped.
            pending = arbiter._pending.get(msg.request_id)
            game_id = pending.game_id if pending is not None else None
            await arbiter.handle_playback_finished(msg)
            # Gate cleared — try dispatching next NPC.
            if game_id is not None:
                await arbiter.try_dispatch_next(game_id)

        async def _on_playback_failed(msg: Any, _ctx: Any) -> None:
            pending = arbiter._pending.get(msg.request_id)
            game_id = pending.game_id if pending is not None else None
            await arbiter.handle_playback_failed(msg)
            if game_id is not None:
                await arbiter.try_dispatch_next(game_id)

        async def _on_speech_payload(msg: Any, _ctx: Any) -> None:
            await ingest_service.ingest_voice(msg)
            # After a new human speech event lands, try dispatching an NPC reply.
            if hasattr(msg, "game_id") and msg.game_id:
                await arbiter.try_dispatch_next(msg.game_id)

        async def _on_vad_started(msg: Any, _ctx: Any) -> None:
            segment_id = getattr(msg, "segment_id", None) or "unknown"
            arbiter.mark_human_speaking(segment_id)

        async def _on_vad_ended(msg: Any, _ctx: Any) -> None:
            segment_id = getattr(msg, "segment_id", None) or "unknown"
            arbiter.clear_human_speaking(segment_id)
            # Human stopped speaking — after STT completes, the speech_event
            # callback will trigger dispatch. But also try now in case no STT
            # event follows (e.g. low-confidence drop).
            if hasattr(msg, "game_id") and msg.game_id:
                await arbiter.try_dispatch_next(msg.game_id)

        master_handlers = MasterHandlers(
            registry=npc_registry,
            on_speak_result=_on_speak_result,
            on_tts_finished=_on_tts_finished,
            on_tts_failed=_on_tts_failed,
            on_playback_finished=_on_playback_finished,
            on_playback_failed=_on_playback_failed,
            on_speech_event_payload=_on_speech_payload,
            on_vad_started=_on_vad_started,
            on_vad_ended=_on_vad_ended,
        )

        host, port_str = settings.MASTER_WS_LISTEN.rsplit(":", 1)
        ws_server = WebsocketsMasterWsServer(
            host=host,
            port=int(port_str),
            psk=settings.MASTER_NPC_PSK.get_secret_value(),
            registry=npc_registry,
            handlers=master_handlers,
        )
        log.info("reactive_voice pipeline wired, WS will listen on %s", settings.MASTER_WS_LISTEN)

    @bot.event
    async def on_ready() -> None:
        guild = discord.Object(id=settings.DISCORD_GUILD_ID)
        await bot.tree.sync(guild=guild)
        log.info("synced slash commands to guild %s", settings.DISCORD_GUILD_ID)
        # on_ready re-fires on reconnect. Engines started on the first ready keep
        # ticking locally across reconnects, so re-running recovery would only
        # duplicate them. Run it once per process.
        if recovery_done.is_set():
            return
        recovery_done.set()
        if ws_server is not None:
            await ws_server.start()
            log.info("master WS server started on %s", settings.MASTER_WS_LISTEN)
        recovered = await recovery.recover_all()
        log.info("recovered %d game(s)", len(recovered))

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_sigterm() -> None:
        log.info("shutdown requested")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_sigterm)

    bot_task = asyncio.create_task(
        bot.start(settings.DISCORD_TOKEN.get_secret_value()),
        name="wolfbot-main",
    )
    await stop_event.wait()

    log.info("stopping engines...")
    await registry.stop_all()
    if ws_server is not None:
        log.info("stopping master WS server...")
        await ws_server.stop()
    log.info("closing bot...")
    await bot.close()
    bot_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await bot_task
    await repo.close()
    log.info("bye")


def cli() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    cli()
