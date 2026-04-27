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

from wolfbot.config import MasterSettings
from wolfbot.persistence.schema import migrate
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discord_service import DiscordBotAdapter, WolfCog
from wolfbot.services.discussion_service import DiscussionService, SqliteSpeechEventStore
from wolfbot.services.game_service import GameService
from wolfbot.services.llm_service import LLMAdapter, make_llm_decider
from wolfbot.services.recovery_service import RecoveryService
from wolfbot.services.timer_service import EngineRegistry

log = logging.getLogger("wolfbot")


async def _run() -> None:
    load_dotenv(".env.master")
    settings = MasterSettings()  # type: ignore[call-arg]
    settings.apply_phase_durations()
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

    # NPC registry is always created so /wolf start can consult it for
    # reactive_voice seat backfill. The WS server (which actually accepts
    # NPC bot connections) is only started when MASTER_NPC_PSK is set.
    from wolfbot.master.npc_registry import InMemoryNpcRegistry

    npc_registry = InMemoryNpcRegistry()

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

    decider = make_llm_decider(settings.gameplay_decider_config())
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

    # ---- Master VC join lifecycle ---------------------------------------
    # Single VC connection; one Master = one guild = at most one active
    # reactive_voice game at a time. Held in a list so closures can rebind.
    master_vc_ref: list[Any] = [None]
    master_vc_lock = asyncio.Lock()

    async def _master_join_vc_for_game(game: Any) -> None:
        """Join the game's VC if reactive_voice + voice_ingest is active.

        Idempotent: returns immediately when the existing connection is
        still healthy. Called from `_on_reactive_phase_enter` and from
        recovery so a Master restart mid-game re-joins. In rounds mode
        Master never joins VC at all.
        """
        if voice_ingest is None:
            return
        if getattr(game, "discussion_mode", None) != "reactive_voice":
            return
        async with master_vc_lock:
            existing = master_vc_ref[0]
            if existing is not None:
                try:
                    if existing.is_connected():
                        return
                except Exception:
                    pass
                master_vc_ref[0] = None
            from discord.ext import voice_recv

            from wolfbot.master.audio_sink import WolfbotAudioSink

            try:
                channel_id = int(game.main_vc_channel_id)
            except (TypeError, ValueError):
                log.warning(
                    "master_vc_channel_id_invalid game=%s value=%r",
                    game.id,
                    game.main_vc_channel_id,
                )
                return
            vc_channel = bot.get_channel(channel_id)
            if vc_channel is None or not isinstance(vc_channel, discord.VoiceChannel):
                log.warning(
                    "master_vc_channel_not_found id=%s", channel_id
                )
                return
            try:
                vc_client = await vc_channel.connect(cls=voice_recv.VoiceRecvClient)
                sink = WolfbotAudioSink(
                    voice_ingest, loop=asyncio.get_running_loop()
                )
                vc_client.listen(sink)
                master_vc_ref[0] = vc_client
                log.info(
                    "master_vc_joined channel=%s game=%s", channel_id, game.id
                )
            except Exception:
                log.warning(
                    "master_vc_join_failed channel=%s",
                    channel_id,
                    exc_info=True,
                )

    async def _master_leave_vc() -> None:
        """Disconnect Master from VC. Idempotent."""
        async with master_vc_lock:
            vc = master_vc_ref[0]
            if vc is None:
                return
            try:
                if vc.is_connected():
                    await vc.disconnect()
                    log.info("master_vc_left")
            except Exception:
                log.exception("master_vc_leave_failed")
            finally:
                master_vc_ref[0] = None

    async def _on_reactive_phase_enter(game_id: str) -> None:
        await _refresh_voice_ingest_cache(game_id)
        # Master joins the game's VC the first time the game enters a
        # public-speech phase. Idempotent so re-entries are no-ops.
        g_for_vc = await repo.load_game(game_id)
        if g_for_vc is not None:
            await _master_join_vc_for_game(g_for_vc)
        # Assign online NPC bots to their game seats so the arbiter can pick them.
        if _npc_registry_ref and _vc_phase_cache[0] is not None:
            from wolfbot.domain.ws_messages import SeatAssigned

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
                # Tell the picked bot to join VC. Unselected NPCs receive
                # nothing and stay idle (no VC).
                if npc_entry.send is not None:
                    try:
                        msg = SeatAssigned(
                            ts=int(asyncio.get_running_loop().time() * 1000),
                            trace_id=f"assign-{game_id}-{seat.seat_no}",
                            npc_id=npc_entry.npc_id,
                            seat_no=seat.seat_no,
                            game_id=game_id,
                            phase_id=phase_id,
                        )
                        await npc_entry.send(msg.model_dump_json())
                    except Exception:
                        log.exception(
                            "seat_assigned_send_failed npc=%s seat=%d",
                            npc_entry.npc_id,
                            seat.seat_no,
                        )
        if _reactive_phase_cb:
            await _reactive_phase_cb[0].try_dispatch_next(game_id)

    async def _on_reactive_game_end(game_id: str) -> None:
        """Release every NPC bot attached to this game so they leave VC.

        Called from `GameService` at natural end + host abort. Sends a
        `seat_released` to each, then clears the registry's assignment
        fields so the bot is available for the next /wolf start.
        """
        if not _npc_registry_ref:
            return
        from wolfbot.domain.ws_messages import SeatReleased

        npc_reg = _npc_registry_ref[0]
        attached = list(npc_reg.assigned_to_game(game_id))
        for entry in attached:
            if entry.send is not None:
                try:
                    msg = SeatReleased(
                        ts=int(asyncio.get_running_loop().time() * 1000),
                        trace_id=f"release-{game_id}-{entry.npc_id}",
                        npc_id=entry.npc_id,
                        game_id=game_id,
                        reason="game_ended",
                    )
                    await entry.send(msg.model_dump_json())
                except Exception:
                    log.exception(
                        "seat_released_send_failed npc=%s game=%s",
                        entry.npc_id,
                        game_id,
                    )
            npc_reg.unassign(entry.npc_id)
            log.info("npc_seat_unassigned npc=%s game=%s", entry.npc_id, game_id)
        # Drop Master's own VC connection too — keeps the bot out of the
        # voice channel between games. Reattaches at the next /wolf start.
        await _master_leave_vc()

    game_service = GameService(
        repo=repo,
        discord=discord_adapter,
        llm=llm_adapter,
        wake=registry,
        on_reactive_phase_enter=_on_reactive_phase_enter,
        on_reactive_game_end=_on_reactive_game_end,
    )
    discord_adapter.set_game_service(game_service)
    llm_adapter.set_game_service(game_service)

    async def _on_speech_recorded(game_id: str) -> None:
        """After a text SpeechEvent is recorded, try dispatching an NPC reply."""
        if _reactive_phase_cb:
            await _reactive_phase_cb[0].try_dispatch_next(game_id)

    # Mirror the voice path's structured analysis on the text channel:
    # one Gemini call per typed message yields the same `addressed_name`
    # / `co_claim` signal so SpeakArbiter can route a text address to
    # the right NPC seat. Wired only when reactive_voice is enabled and a
    # Voice LLM key is present; absent → WolfCog falls back to plain raw
    # capture (the historical behavior).
    text_analyzer: Any = None
    if (
        settings.LLM_DISCUSSION_MODE == "reactive_voice"
        and settings.MASTER_NPC_PSK is not None
        and settings.VOICE_LLM_API_KEY is not None
    ):
        from wolfbot.master.text_analyzer import GeminiTextAnalyzer

        text_analyzer = GeminiTextAnalyzer(
            api_key=settings.VOICE_LLM_API_KEY.get_secret_value(),
            model=settings.VOICE_LLM_MODEL,
        )

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
        npc_registry=npc_registry,
        text_analyzer=text_analyzer,
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
        from wolfbot.master.ingest_service import MasterIngestService
        from wolfbot.master.speak_arbiter import SpeakArbiter
        from wolfbot.master.ws_server import (
            MasterHandlers,
            WebsocketsMasterWsServer,
        )

        _npc_registry_ref.append(npc_registry)

        arbiter = SpeakArbiter(
            repo=repo,
            registry=npc_registry,
            discussion=discussion_service,
        )
        _reactive_phase_cb.append(arbiter)
        recovery._reactive_voice_sweep = arbiter.reactive_voice_recovery_sweep

        async def _reactive_voice_reenter(game_id: str) -> None:
            # On Master restart, reactive_voice games still in
            # DAY_DISCUSSION need their VC joined again before the
            # arbiter starts dispatching. `_master_join_vc_for_game` is
            # idempotent so non-reactive_voice games are a no-op.
            g = await repo.load_game(game_id)
            if g is not None:
                await _master_join_vc_for_game(g)
            await arbiter.try_dispatch_next(game_id)

        recovery._reactive_voice_reenter = _reactive_voice_reenter

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

            async def resolve_addressed_seat(
                self, game_id: str, addressed_name: str
            ) -> int | None:
                from wolfbot.master.ingest_service import resolve_seat_by_name

                seats = await repo.load_seats(game_id)
                players = await repo.load_players(game_id)
                alive = {p.seat_no for p in players if p.alive}
                return resolve_seat_by_name(addressed_name, seats, alive)

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
        if settings.VOICE_LLM_API_KEY is not None:
            from wolfbot.master.stt_service import GeminiAudioAnalyzer
            from wolfbot.master.voice_ingest_client import DirectMasterIngestionClient
            from wolfbot.master.voice_ingest_service import VoiceIngestService

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

            voice_llm = GeminiAudioAnalyzer(
                api_key=settings.VOICE_LLM_API_KEY.get_secret_value(),
                model=settings.VOICE_LLM_MODEL,
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
                stt=voice_llm,
                seat_lookup=_seat_lookup,
                phase_lookup=_phase_lookup,
            )
            log.info("integrated voice-ingest wired (voice_llm_model=%s)",
                     settings.VOICE_LLM_MODEL)

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
        # Master no longer auto-joins VC at startup. Joining is deferred
        # to `_on_reactive_phase_enter` (the first public-speech phase of
        # a reactive_voice game) and to recovery for in-flight games.
        # Hosts who want Master in a non-active VC still rely on starting
        # a game; rounds-mode games never need VC.

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
