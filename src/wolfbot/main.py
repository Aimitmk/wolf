"""Entrypoint. Load env → migrate DB → connect Discord → recover → run."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from typing import Any

import discord
from discord.ext import commands
from dotenv import load_dotenv

from wolfbot.config import MasterSettings
from wolfbot.domain.enums import Phase
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
    # NpcRegistry is created right below; late-bind so `apply_permissions`
    # can server-mute NPC bots on phase changes / death.

    # NPC registry is always created so /wolf start can consult it for
    # reactive_voice seat backfill. The WS server (which actually accepts
    # NPC bot connections) is only started when MASTER_NPC_PSK is set.
    from wolfbot.master.npc_registry import InMemoryNpcRegistry

    npc_registry = InMemoryNpcRegistry()
    discord_adapter.set_npc_registry(npc_registry)

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
            # In reactive_voice mode each NPC bot posts its own utterance
            # directly to VC chat from its own account (see
            # `NpcClient.on_post_chat`), so a duplicate Master-side post
            # would either repeat NPC lines or surface human STT
            # transcripts that the user would rather not see twice. Skip
            # PLAYER_SPEECH unconditionally for this mode; rounds mode
            # still goes through the legacy adapter path.
            if game.discussion_mode == "reactive_voice" and kind == "PLAYER_SPEECH":
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
    # Alive seats as ``(seat_no, display_name)`` so the analyzer LLM
    # can resolve mistranscribed names to a canonical participant.
    _vc_roster: list[tuple[int, str]] = []

    async def _refresh_voice_ingest_cache(game_id: str) -> None:
        """Update seat map and phase cache for the integrated voice-ingest.

        ``_vc_roster`` holds the alive seats in ``(seat_no, display_name)``
        form for the analyzer LLM's name-resolution prompt. The
        ``display_name`` here is the **live VC nickname** as Discord
        renders it for that participant - i.e. what the human speaker
        actually sees on their VC overlay - not the stored
        ``Seat.display_name`` (which for NPC seats is the persona's
        canonical handle and may differ from the Discord bot's
        guild-side nickname). Falls back to the stored value when the
        member is uncached or the bot user_id isn't known yet.
        """
        game = await repo.load_game(game_id)
        if game is None or game.ended_at is not None:
            _vc_phase_cache[0] = None
            return
        from wolfbot.domain.discussion import make_phase_id as _mkpi
        phase_id = _mkpi(game.id, game.day_number, game.phase)
        _vc_phase_cache[0] = (game.id, phase_id)
        seats = await repo.load_seats(game_id)
        players = await repo.load_players(game_id)
        alive_seats = {p.seat_no for p in players if p.alive}
        _vc_seat_map.clear()
        for s in seats:
            if s.discord_user_id and s.seat_no in alive_seats:
                _vc_seat_map[s.discord_user_id] = s.seat_no

        # Resolve each seat's live VC display name. NPC seats have
        # ``discord_user_id=NULL`` in the seats table, so cross-
        # reference NpcRegistry to pick up the bot's actual user id
        # (each NPC bot logs in as its own Discord user).
        try:
            guild = bot.get_guild(int(game.guild_id)) if game.guild_id else None
        except (TypeError, ValueError):
            guild = None
        npc_user_by_seat: dict[int, str] = {}
        if _npc_registry_ref:
            registry = _npc_registry_ref[0]
            for entry in registry.assigned_to_game(game.id):
                if entry.assigned_seat is not None:
                    npc_user_by_seat[entry.assigned_seat] = entry.discord_bot_user_id

        _vc_roster.clear()
        for s in sorted(seats, key=lambda x: x.seat_no):
            if s.seat_no not in alive_seats:
                continue
            user_id_str = s.discord_user_id or npc_user_by_seat.get(s.seat_no)
            live_name: str | None = None
            if guild is not None and user_id_str:
                try:
                    member = guild.get_member(int(user_id_str))
                except (TypeError, ValueError):
                    member = None
                if member is not None:
                    live_name = member.display_name
            _vc_roster.append((s.seat_no, live_name or s.display_name))

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
            from wolfbot.master.voice_recv_dave_patch import (
                apply_dave_decrypt_patch,
            )
            from wolfbot.master.voice_recv_resilience import (
                apply_packet_router_resilience,
            )

            # Patch upstream so a single `OpusError("corrupted stream")` can
            # no longer kill the RX thread — without this, one bad packet
            # silences the entire reactive_voice pipeline for the rest of
            # the game (no STT, no NPC dispatch).
            apply_packet_router_resilience()
            # Layer DAVE (E2EE voice) inner decrypt on top of voice_recv's
            # outer AEAD. Without this, channels with E2EE enabled deliver
            # MLS-encrypted opus that the decoder can't read, manifesting
            # as a flood of `corrupted stream` warnings and zero usable
            # audio (the ``e5660c02f79a`` debug dumps before this patch
            # were the canonical symptom). See voice_recv_dave_patch.py.
            apply_dave_decrypt_patch()

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
                # Wrap with SilenceGeneratorSink so gaps in Discord's
                # opus stream (the user pausing mid-utterance) get
                # padded with synthetic 20ms silence frames. Without
                # this, our PCM buffer concatenates only the spoken
                # frames and the resulting WAV plays back time-
                # compressed — Whisper sees garbled audio and falls
                # back to its boilerplate hallucinations
                # ("ご視聴ありがとうございました").
                vc_client.listen(voice_recv.SilenceGeneratorSink(sink))
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

    async def _push_private_state_snapshot(
        npc_send: Any,
        *,
        npc_id: str,
        game_id: str,
        seat_no: int,
        persona_key: str,
    ) -> None:
        """Build and push a `PrivateStateSnapshot` for the seat the NPC plays.

        Called after `SeatAssigned` so the NPC bot has its full Phase-D
        state (role, partner wolves, role-result histories) before any
        decision is requested. Best-effort — a failed send is logged and
        skipped; the NPC will see the historical empty-state behavior on
        decision requests until the next snapshot opportunity (re-register).
        """
        from wolfbot.master.private_state import (
            build_snapshot_for_seat,
            load_private_state_for_seat,
        )

        try:
            game = await repo.load_game(game_id)
            seats = await repo.load_seats(game_id)
            players = await repo.load_players(game_id)
        except Exception:
            log.exception(
                "private_state_snapshot_load_failed npc=%s seat=%d game=%s",
                npc_id, seat_no, game_id,
            )
            return
        if game is None:
            log.warning(
                "private_state_snapshot_skip_no_game npc=%s seat=%d game=%s",
                npc_id, seat_no, game_id,
            )
            return
        me = next((p for p in players if p.seat_no == seat_no), None)
        if me is None or me.role is None:
            log.warning(
                "private_state_snapshot_skip_no_role npc=%s seat=%d game=%s",
                npc_id, seat_no, game_id,
            )
            return
        # Pull persisted role-specific history (NIGHT_0 random white,
        # past divinations / mediums / guards / wolf chat) from the
        # logs_private + night_actions tables. Without this the seer
        # NPC sees no concrete data to CO with on day 1, even though
        # the strategy block tells them to declare early — which is
        # exactly the day-1 silence the live game hit.
        seer_results, medium_results, guard_history, wolf_chat_history = (
            await load_private_state_for_seat(
                repo,
                game_id=game_id,
                seat_no=seat_no,
                role=me.role,
                players=players,
                seats=seats,
            )
        )
        snapshot = build_snapshot_for_seat(
            npc_id=npc_id,
            game_id=game_id,
            seat_no=seat_no,
            persona_key=persona_key,
            role=me.role,
            day_number=game.day_number,
            players=players,
            seats=seats,
            seer_results=seer_results,
            medium_results=medium_results,
            guard_history=guard_history,
            wolf_chat_history=wolf_chat_history,
            ts=int(asyncio.get_running_loop().time() * 1000),
            trace_id=f"snapshot-{game_id}-{seat_no}",
        )
        try:
            await npc_send(snapshot.model_dump_json())
        except Exception:
            log.exception(
                "private_state_snapshot_send_failed npc=%s seat=%d",
                npc_id, seat_no,
            )

    async def _assign_online_npcs_to_seats(game_id: str) -> int:
        """Pair online NPC bots with unassigned LLM seats and tell them to
        join VC. Idempotent (already-assigned NPCs are skipped). Returns
        the number of NEW assignments dispatched in this call — callers
        use it to decide whether to wait for VC join confirmations.
        """
        if not _npc_registry_ref or _vc_phase_cache[0] is None:
            return 0
        from wolfbot.domain.ws_messages import SeatAssigned

        _game_id, phase_id = _vc_phase_cache[0]
        npc_reg = _npc_registry_ref[0]
        seats = await repo.load_seats(game_id)
        llm_seats = [s for s in seats if s.is_llm]
        online = npc_reg.all_online()
        assigned_npc_ids = {
            e.npc_id for e in online if e.assigned_seat is not None and e.game_id == game_id
        }
        unassigned_npcs = [e for e in online if e.npc_id not in assigned_npc_ids]
        unassigned_seats = [
            s for s in llm_seats
            if not any(e.assigned_seat == s.seat_no and e.game_id == game_id for e in online)
        ]
        dispatched = 0
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
                    dispatched += 1
                except Exception:
                    log.exception(
                        "seat_assigned_send_failed npc=%s seat=%d",
                        npc_entry.npc_id,
                        seat.seat_no,
                    )
                    continue
                # Phase-D: push the seat's full private state right after
                # SeatAssigned so the NPC bot can rebuild its in-memory
                # game state before the first decision request lands.
                # This may skip with `private_state_snapshot_skip_no_role`
                # when called pre-SETUP (the `/wolf start` path runs before
                # role assignment); the self-heal below re-pushes once
                # roles are visible.
                await _push_private_state_snapshot(
                    npc_entry.send,
                    npc_id=npc_entry.npc_id,
                    game_id=game_id,
                    seat_no=seat.seat_no,
                    persona_key=npc_entry.persona_key or "",
                )
        # Self-heal: re-push snapshots for every NPC currently assigned to
        # this game, not just the ones we touched above. The first push
        # from `/wolf start`'s `_on_reactive_game_start` runs before
        # `plan_setup` writes roles, so the snapshot is silently dropped
        # by `_push_private_state_snapshot`'s "no role" guard. Without a
        # second push the NPC's `game_states[game_id]` stays empty for
        # the entire game — every DecideVoteRequest /
        # DecideNightActionRequest / WolfChatRequest then falls back to
        # `target=None` because the decision handler short-circuits on
        # missing state. Re-pushing on every phase entry is idempotent
        # (the NPC client overwrites `game_states[game_id]` from the new
        # snapshot) and only adds one small WS frame per assigned NPC
        # per phase change. Safe-by-default also covers Master restarts:
        # a re-attached NPC gets fresh state at the next phase enter.
        for npc_entry in npc_reg.all_online():
            if npc_entry.assigned_seat is None or npc_entry.game_id != game_id:
                continue
            if npc_entry.send is None:
                continue
            await _push_private_state_snapshot(
                npc_entry.send,
                npc_id=npc_entry.npc_id,
                game_id=game_id,
                seat_no=npc_entry.assigned_seat,
                persona_key=npc_entry.persona_key or "",
            )
        return dispatched

    async def _wait_for_npcs_in_vc(expected_count: int, timeout: float = 5.0) -> None:
        """Block until `expected_count` bots are visible in Master's VC.

        After SeatAssigned dispatch, NPCs join VC asynchronously (Discord
        connect ~1-1.5s each). Without this wait, the SETUP_COMPLETE
        narration starts speaking into an empty channel. We poll the
        VoiceChannel.members list (counting bots, excluding the Master
        itself) and bail on timeout so a single slow NPC doesn't stall
        game start indefinitely.
        """
        if expected_count <= 0:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            vc = master_vc_ref[0]
            channel = getattr(vc, "channel", None) if vc is not None else None
            members = getattr(channel, "members", None)
            if members is not None:
                bot_user = bot.user
                count = sum(
                    1 for m in members if getattr(m, "bot", False) and m != bot_user
                )
                if count >= expected_count:
                    log.info("npc_vc_join_confirmed count=%d", count)
                    return
            await asyncio.sleep(0.25)
        log.info(
            "npc_vc_join_timeout expected=%d after=%.1fs", expected_count, timeout
        )

    async def _on_reactive_game_start(game_id: str) -> None:
        """Pre-game NPC + Master VC setup, called from `/wolf start`.

        Without this, NPCs only join VC on DAY_DISCUSSION entry — too
        late for the SETUP_COMPLETE / day-1 PHASE_CHANGE narration to
        be heard from a populated channel. We:
          1. Cache phase_id (needed for SeatAssigned),
          2. Master joins VC (so it can voice the announcement),
          3. Assign online NPCs to LLM seats + dispatch SeatAssigned
             so they join VC,
          4. Wait briefly for the NPC VC joins to land.
        Idempotent — `_on_reactive_phase_enter` re-invokes the same
        helpers later and finds nothing left to do.
        """
        g = await repo.load_game(game_id)
        if g is None or g.ended_at is not None:
            return
        if g.discussion_mode != "reactive_voice":
            return
        await _refresh_voice_ingest_cache(game_id)
        await _master_join_vc_for_game(g)
        dispatched = await _assign_online_npcs_to_seats(game_id)
        if dispatched > 0:
            await _wait_for_npcs_in_vc(dispatched)

    async def _on_reactive_phase_enter(game_id: str) -> None:
        await _refresh_voice_ingest_cache(game_id)
        # Master joins the game's VC the first time the game enters a
        # public-speech phase. Idempotent so re-entries are no-ops.
        g_for_vc = await repo.load_game(game_id)
        # Bail when the game is already ended — this callback can run a
        # few hundred ms after `/wolf abort` if a transition's
        # `_dispatch_submissions` was in flight when abort fired. Without
        # this guard Master rejoins VC right after abort just to drop
        # the call again seconds later, surfacing as the "Master came
        # back" flicker in the user's voice channel.
        if g_for_vc is None or g_for_vc.ended_at is not None:
            return
        await _master_join_vc_for_game(g_for_vc)
        # Seed the phase_baseline sentinel so SpeakArbiter's
        # `rebuild_public_state` has an alive-seat baseline to fold
        # against. In rounds mode this happens inside
        # `submit_llm_discussion_rounds`, but reactive_voice skips
        # that batch entirely — without seeding here, the arbiter
        # silently no-ops on every dispatch attempt because the
        # rebuilt state is None. begin_phase_if_absent is idempotent
        # across re-entries / recovery.
        if g_for_vc.discussion_mode == "reactive_voice":
            players_for_baseline = await repo.load_players(game_id)
            alive_seat_nos = sorted(
                p.seat_no for p in players_for_baseline if p.alive
            )
            try:
                await discussion_service.begin_phase_if_absent(
                    game_id=game_id,
                    day=g_for_vc.day_number,
                    phase=g_for_vc.phase,
                    alive_seat_nos=alive_seat_nos,
                )
            except Exception:
                log.exception(
                    "phase_baseline_seed_failed game=%s phase=%s",
                    game_id,
                    g_for_vc.phase,
                )
        # Assign online NPC bots to their game seats so the arbiter can
        # pick them. No-op when `/wolf start`'s pre-game callback already
        # claimed them.
        await _assign_online_npcs_to_seats(game_id)
        if _reactive_phase_cb:
            await _reactive_phase_cb[0].try_dispatch_next(game_id)

    async def _on_reactive_game_end(game_id: str) -> None:
        """Release every NPC bot so they leave VC.

        Called from `GameService` at natural end + host abort. Sends a
        `seat_released` to *every online NPC* — not just those whose
        registry row still pins them to this game — so any bot that
        ended up in VC (e.g. via a previous abort that left a stale WS
        connection or a dropped assignment) is reliably evicted before
        the next /wolf start. Idempotent on the NPC side.
        """
        if not _npc_registry_ref:
            return
        from wolfbot.domain.ws_messages import SeatReleased

        npc_reg = _npc_registry_ref[0]
        # Union: bots assigned to *this* game + every online bot whose
        # registry row carries no game assignment but might still be in
        # VC. We don't filter strictly because abort must be a sweep, not
        # a precision strike.
        attached = {e.npc_id: e for e in npc_reg.assigned_to_game(game_id)}
        for e in npc_reg.all_online():
            attached.setdefault(e.npc_id, e)
        for entry in attached.values():
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
        # Drop in-memory dispatcher / arbiter state for this game so a
        # long-lived Master process doesn't accumulate stale pending
        # futures + playback gates across many games. DB rows are kept
        # (export / replay depends on them); only the live dicts are
        # swept here. Best-effort: missing references (rounds-mode build
        # without dispatcher / arbiter wired) are silent no-ops.
        dispatcher = getattr(llm_adapter, "_npc_decision_dispatcher", None)
        if dispatcher is not None and hasattr(dispatcher, "cleanup_game"):
            try:
                dispatcher.cleanup_game(game_id)
            except Exception:
                log.exception(
                    "decision_dispatcher_cleanup_failed game=%s", game_id
                )
        if _reactive_phase_cb:
            arbiter_ref = _reactive_phase_cb[0]
            if hasattr(arbiter_ref, "cleanup_game"):
                try:
                    arbiter_ref.cleanup_game(game_id)
                except Exception:
                    log.exception(
                        "speak_arbiter_cleanup_failed game=%s", game_id
                    )
        # Drop Master's own VC connection too — keeps the bot out of the
        # voice channel between games. Reattaches at the next /wolf start.
        await _master_leave_vc()

    async def _on_game_end_finalize(game_id: str) -> None:
        """Export the finished/aborted game to viewer-compatible JSON.

        Joins SQLite + ``logs/llm_calls/{game_id}/*.jsonl`` into one file
        under ``viewer/games/{game_id}.json``. The viewer auto-discovers
        the most-recent file in that directory, so a finished game can
        be reviewed by simply running ``cd viewer && pnpm dev`` — no
        separate export step or env var. Errors here MUST NOT prevent
        end-of-game cleanup; ``GameService._run_finalize_hook`` already
        wraps this in try/except.
        """
        from wolfbot.services.game_export import export_game

        await export_game(
            game_id=game_id,
            db_path=settings.WOLFBOT_DB_PATH,
        )

    game_service = GameService(
        repo=repo,
        discord=discord_adapter,
        llm=llm_adapter,
        wake=registry,
        on_reactive_phase_enter=_on_reactive_phase_enter,
        on_reactive_game_end=_on_reactive_game_end,
        on_game_end_finalize=_on_game_end_finalize,
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
        on_reactive_game_start=_on_reactive_game_start,
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

        # Late-bound holder so arbiter can call into master_tts (built
        # below) without reordering construction. GameService is already
        # in scope so we can pin it immediately.
        _master_tts_holder: list[Any] = []
        _game_service_holder: list[Any] = [game_service]

        async def _runoff_announce(seat: Any) -> None:
            """Levi voice-introduces a tied runoff candidate before TTS."""
            from wolfbot.master.narration import render_runoff_candidate_intro

            if not _master_tts_holder:
                return
            tts = _master_tts_holder[0]
            try:
                async with tts.suppress_npc_dispatch(arbiter):
                    await tts.speak(render_runoff_candidate_intro(seat))
            except Exception:
                log.exception(
                    "runoff_candidate_intro_failed seat=%d",
                    getattr(seat, "seat_no", -1),
                )

        def _runoff_wake(game_id: str) -> None:
            if not _game_service_holder:
                return
            try:
                _game_service_holder[0].wake.wake(game_id)
            except Exception:
                log.exception("runoff_wake_invocation_failed game=%s", game_id)

        arbiter = SpeakArbiter(
            repo=repo,
            registry=npc_registry,
            discussion=discussion_service,
            runoff_announce=_runoff_announce,
            runoff_wake=_runoff_wake,
        )
        _reactive_phase_cb.append(arbiter)
        recovery._reactive_voice_sweep = arbiter.reactive_voice_recovery_sweep

        # Phase-D: NPC bot dispatcher for vote / night-action decisions.
        # Wired into LLMAdapter so reactive_voice games skip the gameplay
        # decider and ask the NPC bot for its own seat's vote / night
        # action via WS.
        from wolfbot.master.decision_dispatcher import NpcDecisionDispatcher
        from wolfbot.master.wolf_chat_broker import WolfChatBroker

        decision_dispatcher = NpcDecisionDispatcher(
            registry=npc_registry,
            now_ms=lambda: int(time.time() * 1000),
        )
        llm_adapter._npc_decision_dispatcher = decision_dispatcher

        async def _post_to_wolves_channel(game_id: str, text: str) -> None:
            game = await repo.load_game(game_id)
            if game is None:
                return
            await discord_adapter.post_wolves_chat(game, text, kind="WOLF_CHAT")

        wolf_chat_broker = WolfChatBroker(
            registry=npc_registry,
            repo=repo,
            post_to_wolves_channel=_post_to_wolves_channel,
            now_ms=lambda: int(time.time() * 1000),
        )

        # Phase-D: state pusher fans out seer/medium/guard results,
        # alive_changed, and day_advanced PrivateStateUpdates to NPC
        # bots after each transition. Wired into GameService via the
        # late-binding setter (the pusher needs npc_registry which is
        # only available here, but GameService was constructed earlier).
        from wolfbot.master.phase_d_state_pusher import PhaseDStatePusher

        phase_d_pusher = PhaseDStatePusher(
            repo=repo,
            registry=npc_registry,
            now_ms=lambda: int(time.time() * 1000),
        )
        game_service.set_phase_d_state_pusher(phase_d_pusher)

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

        # ---- Master narration (Levi TTS in VC + long content to VC chat) ----
        # Built once per process; the narrator callback is installed on
        # `discord_adapter` so `post_public` / `post_morning` route through
        # it whenever the active game is in reactive_voice mode.
        from wolfbot.master.narration import (
            NarrationContext,
            render_master_narration,
        )
        from wolfbot.master.tts_playback import MasterTtsPlayback
        from wolfbot.npc.tts import VoicevoxTtsService

        master_tts = MasterTtsPlayback(
            tts=VoicevoxTtsService(
                base_url=settings.MASTER_VOICEVOX_URL,
                default_speaker=settings.MASTER_TTS_VOICE_ID,
            ),
            voice_id=str(settings.MASTER_TTS_VOICE_ID),
            vc_ref=master_vc_ref,
        )
        # Hand the live MasterTtsPlayback to the arbiter's runoff_announce
        # closure (built earlier with a late-binding holder so we didn't
        # need to reorder construction).
        _master_tts_holder.append(master_tts)

        async def _post_to_vc_chat(game: Any, text: str) -> None:
            """Post `text` to the VC's attached text chat.

            In reactive_voice mode Master is forbidden from leaking text
            into the guild's main text channel — every word goes to DM
            or VC. Discord voice channels (since 2022) carry an attached
            text chat reachable at the same channel id; both
            ``VoiceChannel`` and ``TextChannel`` are ``Messageable``. If
            the VC channel can't be resolved we drop the message rather
            than fall back to main text — silence beats a leak."""
            try:
                channel_id: int | None = int(game.main_vc_channel_id)
            except (TypeError, ValueError):
                channel_id = None
            channel = bot.get_channel(channel_id) if channel_id else None
            if not isinstance(
                channel, discord.TextChannel | discord.VoiceChannel
            ):
                log.warning(
                    "vc_chat_post_no_channel game=%s vc=%s",
                    game.id,
                    game.main_vc_channel_id,
                )
                return
            try:
                await channel.send(text)
            except Exception:
                log.exception(
                    "vc_chat_post_failed game=%s channel=%s",
                    game.id,
                    getattr(channel, "id", None),
                )

        async def _push_deadline_after_narration(game_id: str) -> None:
            """Push the active phase's `deadline_epoch` to `now + duration`.

            Called from the narrator after TTS playback finishes. Without
            this, mock-mode short phases (vote=6s) get fully consumed by a
            15s TTS announcement and the engine advances before any NPC /
            human can act. Real-time phases also benefit — the phase clock
            now starts from when Levi finishes speaking.

            Only pushes forward (never backward), and only when the phase
            currently has a deadline (transient SETUP / NIGHT_0 /
            WAITING_HOST_DECISION / GAME_OVER skip).
            """
            from wolfbot.domain.durations import current_phase_durations

            fresh = await repo.load_game(game_id)
            if fresh is None or fresh.ended_at is not None:
                return
            if fresh.deadline_epoch is None:
                return
            durations = current_phase_durations()
            duration_for: dict[Phase, int] = {
                Phase.DAY_DISCUSSION: durations.discussion_for_day(
                    max(1, fresh.day_number)
                ),
                Phase.DAY_VOTE: durations.vote,
                Phase.DAY_RUNOFF: durations.runoff,
                Phase.DAY_RUNOFF_SPEECH: durations.runoff_speech_grace,
                Phase.NIGHT: durations.night,
            }
            duration = duration_for.get(fresh.phase)
            if duration is None:
                return
            new_deadline = int(time.time()) + duration
            if new_deadline <= fresh.deadline_epoch:
                # TTS finished within the original budget — no push needed.
                return
            await repo.set_deadline(fresh.id, new_deadline)
            log.info(
                "master_tts_deadline_pushed game=%s phase=%s "
                "old=%d new=%d delta=+%ds",
                fresh.id,
                fresh.phase.value,
                fresh.deadline_epoch,
                new_deadline,
                new_deadline - fresh.deadline_epoch,
            )

        async def _master_narrate(game: Any, kind: str, text: str) -> bool:
            """Public-post narrator: voice + VC chat in reactive_voice mode.

            Returns True when the post has been fully handled (the adapter
            must skip its main-text-channel default). Returns False to
            let the legacy text path run — used when not in reactive_voice
            mode, when Master isn't actually in VC, or when no narration
            template applies.
            """
            if game.discussion_mode != "reactive_voice":
                return False
            # Defensive: an in-flight transition's `post_public` can land
            # *after* `/wolf abort` has already torn the game down. We
            # must not lazy-join VC for a corpse game — that's exactly
            # the "Master came back after abort" flicker.
            fresh = await repo.load_game(game.id)
            if fresh is None or fresh.ended_at is not None:
                return False
            if master_vc_ref[0] is None:
                # Lazy-join: SETUP_COMPLETE and the day-1 PHASE_CHANGE are
                # posted *before* `_on_reactive_phase_enter` runs, so on the
                # very first transition Master isn't in VC yet. Join here
                # so opening narrations are voiced instead of falling
                # through to text. Idempotent for later log entries.
                await _master_join_vc_for_game(game)
                if master_vc_ref[0] is None:
                    # Join failed (voice_ingest off, channel resolution
                    # failed, etc.) — fall through to text.
                    return False
            from wolfbot.domain.models import LogEntry as _LogEntry

            players = await repo.load_players(game.id)
            seats = await repo.load_seats(game.id)
            ctx = NarrationContext(
                day_number=game.day_number,
                phase=game.phase,
                alive_count=sum(1 for p in players if p.alive),
                seats_by_no={s.seat_no: s for s in seats},
            )
            # Synthesize an entry-shaped object for the narrator. The
            # caller (DiscordAdapter.post_public / post_morning) only
            # gives us text + kind, so we reconstruct what the narrator
            # needs without hitting the public_logs table.
            actor_seat: int | None = None
            if kind in ("EXECUTION", "MORNING"):
                # Recover actor_seat from the public_log row that
                # GameService.apply_transition just wrote so MORNING
                # narration can name the deceased seat. The matching
                # row's text equals the text the adapter received here
                # (LogEntry.text round-trips). Walk the most recent
                # entries newest-first.
                try:
                    rows = await repo.load_public_logs(game.id, limit=10)
                except Exception:
                    rows = []
                for row in reversed(rows):
                    if row.get("kind") == kind and row.get("text") == text:
                        actor_seat = row.get("actor_seat")
                        break
            faux_entry = _LogEntry(
                game_id=game.id,
                day=game.day_number,
                phase=game.phase,
                kind=kind,
                actor_seat=actor_seat,
                visibility="PUBLIC",
                text=text,
                created_at=0,
            )
            output = render_master_narration(faux_entry, ctx)
            if output.is_silent():
                # No template matched — let the caller fall through to
                # the legacy main-text post so the message isn't dropped.
                return False
            if output.chat_text:
                await _post_to_vc_chat(game, output.chat_text)
            if output.voice_text:
                async with master_tts.suppress_npc_dispatch(arbiter):
                    await master_tts.speak(output.voice_text)
                # Phase deadlines are committed in apply_transition based
                # on `now` at plan_next time, so a 15s TTS playback eats
                # straight into the next phase's budget — in mock mode the
                # vote/runoff window can elapse before NPCs even start.
                # After the announcement plays, push the deadline forward
                # so the phase clock starts from when Levi finishes.
                await _push_deadline_after_narration(game.id)
                # After Master narrates, give NPC dispatch a kick — a
                # PHASE_CHANGE into DAY_DISCUSSION should immediately
                # invite an NPC reply.
                if game.phase in (
                    Phase.DAY_DISCUSSION, Phase.DAY_RUNOFF_SPEECH
                ):
                    try:
                        await arbiter.try_dispatch_next(game.id)
                    except Exception:
                        log.exception(
                            "post_narration_dispatch_failed game=%s", game.id
                        )
            return True

        discord_adapter.set_narrator(_master_narrate)

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
            # Resolve game_id BEFORE handle_tts_failed pops `_pending`,
            # otherwise the post-pop lookup is always None and the next
            # NPC never gets dispatched. Production hit: Jonas' second
            # speech timed out on VOICEVOX, the gate cleared, but the
            # arbiter stalled silently for the rest of the phase
            # because game_id was looked up after the pop.
            pending = arbiter._pending.get(msg.request_id)
            game_id = pending.game_id if pending is not None else None
            await arbiter.handle_tts_failed(msg)
            # Gate cleared — try dispatching next NPC.
            if game_id is not None:
                await arbiter.try_dispatch_next(game_id)

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

        async def _on_vote_decision(msg: Any, _ctx: Any) -> None:
            await decision_dispatcher.on_vote_decision(msg)

        async def _on_night_action_decision(msg: Any, _ctx: Any) -> None:
            await decision_dispatcher.on_night_action_decision(msg)

        async def _on_wolf_chat_send(msg: Any, _ctx: Any) -> None:
            # Two consumers run for every wolf_chat_send:
            # 1) Broker: persist as WOLF_CHAT private log + broadcast a
            #    `wolf_chat` PrivateStateUpdate to other live wolves.
            # 2) Dispatcher: resolve the matching pending future when
            #    the line was prompted by a Master-issued `WolfChatRequest`.
            # Order matters — let the broker run first so by the time
            # the dispatcher resolves the future and the wolf-chat
            # gather() returns, every other wolf NPC's mirror is
            # already updated.
            await wolf_chat_broker.handle_wolf_chat_send(msg)
            await decision_dispatcher.on_wolf_chat_send(msg)

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
            on_vote_decision=_on_vote_decision,
            on_night_action_decision=_on_night_action_decision,
            on_wolf_chat_send=_on_wolf_chat_send,
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
        # Voice STT is wired iff a credential exists for the chosen
        # provider. Gemini path needs ``VOICE_LLM_API_KEY``; Groq path
        # needs ``GROQ_STT_API_KEY`` plus an OpenAI-compatible analyzer
        # (gameplay key reused).
        _voice_stt_credentialed = (
            (settings.VOICE_STT_PROVIDER == "gemini" and settings.VOICE_LLM_API_KEY is not None)
            or (
                settings.VOICE_STT_PROVIDER == "groq"
                and settings.GROQ_STT_API_KEY is not None
                and settings.GAMEPLAY_LLM_API_KEY is not None
            )
        )
        if _voice_stt_credentialed:
            from wolfbot.master.stt_service import (
                GeminiAudioAnalyzer,
                GroqWhisperAudioAnalyzer,
            )
            from wolfbot.master.voice_ingest_client import DirectMasterIngestionClient
            from wolfbot.master.voice_ingest_service import (
                VoiceIngestConfig,
                VoiceIngestService,
            )

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

            voice_llm: Any
            if settings.VOICE_STT_PROVIDER == "groq":
                # Reuse gameplay key for the analyzer step. Validator
                # already guarantees both keys are present.
                assert settings.GROQ_STT_API_KEY is not None
                assert settings.GAMEPLAY_LLM_API_KEY is not None
                analyzer_base_url = (
                    settings.GAMEPLAY_LLM_BASE_URL or "https://api.x.ai/v1"
                )
                voice_llm = GroqWhisperAudioAnalyzer(
                    groq_api_key=settings.GROQ_STT_API_KEY.get_secret_value(),
                    groq_model=settings.GROQ_STT_MODEL,
                    groq_base_url=settings.GROQ_STT_BASE_URL,
                    analyzer_api_key=settings.GAMEPLAY_LLM_API_KEY.get_secret_value(),
                    analyzer_model=settings.GAMEPLAY_LLM_MODEL,
                    analyzer_base_url=analyzer_base_url,
                )
            else:
                assert settings.VOICE_LLM_API_KEY is not None
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

            def _roster_lookup() -> list[tuple[int, str]]:
                """Snapshot of alive seats for grounding the STT analyzer.

                Read out of the ``_vc_roster`` cache populated by
                ``_refresh_voice_ingest_cache`` so we don't hit the DB
                on every speech segment.
                """
                return list(_vc_roster)

            voice_ingest = VoiceIngestService(
                registry_view=_RegistryViewAdapter(),
                master_client=direct_client,
                stt=voice_llm,
                seat_lookup=_seat_lookup,
                phase_lookup=_phase_lookup,
                config=VoiceIngestConfig(
                    pre_stt_min_rms=settings.VOICE_PRE_STT_MIN_RMS,
                    pre_stt_min_duration_ms=settings.VOICE_PRE_STT_MIN_DURATION_MS,
                ),
                roster_lookup=_roster_lookup,
            )
            if settings.VOICE_STT_PROVIDER == "groq":
                log.info(
                    "integrated voice-ingest wired (provider=groq stt_model=%s analyzer=%s)",
                    settings.GROQ_STT_MODEL,
                    settings.GAMEPLAY_LLM_MODEL,
                )
            else:
                log.info(
                    "integrated voice-ingest wired (provider=gemini voice_llm_model=%s)",
                    settings.VOICE_LLM_MODEL,
                )

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
