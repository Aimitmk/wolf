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
    intents.voice_states = True
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
    # Optional NPC registry reference for seat assignment on phase enter.
    _npc_registry_ref: list[Any] = []
    # voice-ingest seat/phase caches (populated when integrated ingest is active)
    _vc_seat_map: dict[str, int] = {}
    _vc_phase_cache: list[tuple[str, str] | None] = [None]

    async def _refresh_voice_ingest_cache(game_id: str) -> None:
        """Update seat map and phase cache for the integrated voice-ingest."""
        game = await repo.load_game(game_id)
        if game is None or game.ended_at is not None:
            _vc_phase_cache[0] = None
            return
        from wolfbot.domain.discussion import make_phase_id as _mkpi
        phase_id = _mkpi(game.id, game.day_number, game.phase)
        _vc_phase_cache[0] = (game.id, phase_id)
        seats = await repo.load_seats(game_id)
        players = await repo.load_players(game_id)
        _vc_seat_map.clear()
        for s in seats:
            if s.discord_user_id and any(p.seat_no == s.seat_no and p.alive for p in players):
                _vc_seat_map[s.discord_user_id] = s.seat_no

    async def _on_reactive_phase_enter(game_id: str) -> None:
        await _refresh_voice_ingest_cache(game_id)
        # Assign online NPC bots to their game seats so the arbiter can pick them.
        if _npc_registry_ref and _vc_phase_cache[0] is not None:
            _game_id, phase_id = _vc_phase_cache[0]
            npc_reg = _npc_registry_ref[0]
            seats = await repo.load_seats(game_id)
            llm_seats = [s for s in seats if s.is_llm]
            online = npc_reg.all_online()
            # Pair each online NPC bot with an unassigned LLM seat (round-robin).
            assigned_npc_ids = {
                e.npc_id for e in online if e.assigned_seat is not None and e.game_id == game_id}
            unassigned_npcs = [
                e for e in online if e.npc_id not in assigned_npc_ids]
            unassigned_seats = [s for s in llm_seats if not any(
                e.assigned_seat == s.seat_no and e.game_id == game_id for e in online
            )]
            for npc_entry, seat in zip(unassigned_npcs, unassigned_seats, strict=False):
                npc_reg.assign(npc_entry.npc_id, seat=seat.seat_no,
                               game_id=game_id, phase_id=phase_id)
                log.info("npc_seat_assigned npc=%s seat=%d game=%s",
                         npc_entry.npc_id, seat.seat_no, game_id)
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
    bot.tree.add_command(cog.wolf, guild=discord.Object(
        id=settings.DISCORD_GUILD_ID))

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
    voice_ingest: Any = None
    if settings.MASTER_NPC_PSK is not None:
        from wolfbot.services.master_ingest_service import MasterIngestService
        from wolfbot.services.master_ws_server import (
            MasterHandlers,
            WebsocketsMasterWsServer,
        )
        from wolfbot.services.npc_registry import InMemoryNpcRegistry
        from wolfbot.services.speak_arbiter import SpeakArbiter

        npc_registry = InMemoryNpcRegistry()
        _npc_registry_ref.append(npc_registry)

        arbiter = SpeakArbiter(
            repo=repo,
            registry=npc_registry,
            discussion=discussion_service,
        )
        _reactive_phase_cb.append(arbiter)
        recovery._reactive_voice_sweep = arbiter.reactive_voice_recovery_sweep
        recovery._reactive_voice_reenter = arbiter.try_dispatch_next

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
                log.info("speak_result_accepted npc=%s game=%s",
                         msg.npc_id, g.id)

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
            # Finalize the STT gate for this segment before dispatching.
            segment_id = getattr(msg, "segment_id", None)
            if segment_id:
                arbiter.finalize_stt(segment_id)
            # After a new human speech event lands, try dispatching an NPC reply.
            if hasattr(msg, "game_id") and msg.game_id:
                await arbiter.try_dispatch_next(msg.game_id)

        async def _on_stt_failed(msg: Any, _ctx: Any) -> None:
            segment_id = getattr(msg, "segment_id", None)
            if segment_id:
                arbiter.finalize_stt(segment_id)
            # Gate cleared — try dispatching even though the STT failed.
            game_id = getattr(msg, "game_id", None)
            if game_id:
                await arbiter.try_dispatch_next(game_id)

        async def _on_vad_started(msg: Any, _ctx: Any) -> None:
            segment_id = getattr(msg, "segment_id", None) or "unknown"
            arbiter.mark_human_speaking(segment_id)

        async def _on_vad_ended(msg: Any, _ctx: Any) -> None:
            segment_id = getattr(msg, "segment_id", None) or "unknown"
            # Keep the human-speaking gate held until STT completes.
            # mark_pending_stt records the segment for finalization timeout.
            arbiter.mark_pending_stt(segment_id)

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
            on_stt_failed=_on_stt_failed,
        )

        host, port_str = settings.MASTER_WS_LISTEN.rsplit(":", 1)
        ws_server = WebsocketsMasterWsServer(
            host=host,
            port=int(port_str),
            psk=settings.MASTER_NPC_PSK.get_secret_value(),
            registry=npc_registry,
            handlers=master_handlers,
        )
        log.info("reactive_voice pipeline wired, WS will listen on %s",
                 settings.MASTER_WS_LISTEN)

        # ---- integrated voice-ingest (STT runs in Master process) ----
        # Instead of a separate voice-ingest process, Master joins VC itself
        # and pipes audio through VoiceIngestService → DirectMasterIngestionClient
        # → arbiter/ingest_service, all in-process.
        if settings.GEMINI_API_KEY is not None:
            from wolfbot.services.stt_service import GeminiAudioAnalyzer
            from wolfbot.services.voice_ingest_client import DirectMasterIngestionClient
            from wolfbot.services.voice_ingest_service import VoiceIngestService

            # Direct callbacks (no WS ctx needed)
            async def _direct_vad_started(msg: Any) -> None:
                segment_id = getattr(msg, "segment_id", None) or "unknown"
                arbiter.mark_human_speaking(segment_id)

            async def _direct_vad_ended(msg: Any) -> None:
                segment_id = getattr(msg, "segment_id", None) or "unknown"
                arbiter.mark_pending_stt(segment_id)

            async def _direct_speech_payload(msg: Any) -> None:
                await ingest_service.ingest_voice(msg)
                segment_id = getattr(msg, "segment_id", None)
                if segment_id:
                    arbiter.finalize_stt(segment_id)
                if hasattr(msg, "game_id") and msg.game_id:
                    await arbiter.try_dispatch_next(msg.game_id)

            async def _direct_stt_failed(msg: Any) -> None:
                segment_id = getattr(msg, "segment_id", None)
                if segment_id:
                    arbiter.finalize_stt(segment_id)
                game_id = getattr(msg, "game_id", None)
                if game_id:
                    await arbiter.try_dispatch_next(game_id)

            direct_client = DirectMasterIngestionClient(
                on_vad_started=_direct_vad_started,
                on_vad_ended=_direct_vad_ended,
                on_speech_event_payload=_direct_speech_payload,
                on_stt_failed=_direct_stt_failed,
            )

            gemini_stt = GeminiAudioAnalyzer(
                api_key=settings.GEMINI_API_KEY.get_secret_value(),
                model=settings.GEMINI_MODEL,
            )

            # NpcRegistryView adapter: InMemoryNpcRegistry → NpcRegistryView
            class _RegistryViewAdapter:
                def is_npc(self, discord_user_id: str) -> bool:
                    return discord_user_id in npc_registry.discord_bot_user_ids()

                def npc_user_ids(self) -> set[str]:
                    return npc_registry.discord_bot_user_ids()

            def _seat_lookup(discord_user_id: str) -> int | None:
                """Resolve a Discord user ID to their seat number."""
                return _vc_seat_map.get(discord_user_id)

            def _phase_lookup() -> tuple[str, str] | None:
                return _vc_phase_cache[0]

            voice_ingest = VoiceIngestService(
                registry_view=_RegistryViewAdapter(),
                master_client=direct_client,
                stt=gemini_stt,
                seat_lookup=_seat_lookup,
                phase_lookup=_phase_lookup,
            )
            log.info("integrated voice-ingest wired (Gemini model=%s)",
                     settings.GEMINI_MODEL)

    @bot.event
    async def on_ready() -> None:
        guild = discord.Object(id=settings.DISCORD_GUILD_ID)
        await bot.tree.sync(guild=guild)
        log.info("synced slash commands to guild %s",
                 settings.DISCORD_GUILD_ID)
        # on_ready re-fires on reconnect. Engines started on the first ready keep
        # ticking locally across reconnects, so re-running recovery would only
        # duplicate them. Run it once per process.
        if recovery_done.is_set():
            return
        recovery_done.set()
        if ws_server is not None:
            await ws_server.start()
            log.info("master WS server started on %s",
                     settings.MASTER_WS_LISTEN)
        # Join VC and start listening via discord-ext-voice-recv AudioSink.
        if voice_ingest is not None:
            from discord.ext import voice_recv

            from wolfbot.services.audio_sink import WolfbotAudioSink

            vc_channel = bot.get_channel(settings.MAIN_VOICE_CHANNEL_ID)
            if vc_channel is not None and isinstance(vc_channel, discord.VoiceChannel):
                try:
                    vc_client = await vc_channel.connect(cls=voice_recv.VoiceRecvClient)
                    sink = WolfbotAudioSink(
                        voice_ingest, loop=asyncio.get_running_loop())
                    vc_client.listen(sink)
                    log.info("master_vc_joined channel=%s, audio_sink active",
                             settings.MAIN_VOICE_CHANNEL_ID)
                except Exception:
                    log.warning("master_vc_join_failed channel=%s",
                                settings.MAIN_VOICE_CHANNEL_ID, exc_info=True)
            else:
                log.warning("voice_channel_not_found id=%s",
                            settings.MAIN_VOICE_CHANNEL_ID)

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
