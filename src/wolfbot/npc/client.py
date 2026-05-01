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
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from wolfbot.domain.ws_messages import (
    DecideNightActionRequest,
    DecideVoteRequest,
    Heartbeat,
    LogicPacket,
    NightActionDecision,
    NpcRegister,
    NpcRegistered,
    PlaybackAuthorized,
    PlaybackFailed,
    PlaybackFinished,
    PlaybackRejected,
    PrivateStateSnapshot,
    PrivateStateUpdate,
    SeatAssigned,
    SeatReleased,
    SetMuteState,
    SpeakRequest,
    TtsFailed,
    TtsFinished,
    VoteDecision,
    WolfChatRequest,
    WolfChatSend,
)
from wolfbot.npc.decision_service import (
    _NIGHT_SCHEMA,
    _VOTE_SCHEMA,
    _WOLF_CHAT_SCHEMA,
    DecisionLLM,
    build_night_prompt,
    build_vote_prompt,
    build_wolf_chat_prompt,
    parse_decision,
    parse_wolf_chat_text,
)
from wolfbot.npc.game_state import NpcGameState, apply_update, state_from_snapshot
from wolfbot.npc.playback import (
    VoicePlayback,
    VoicePlaybackError,
)
from wolfbot.npc.speech_service import NpcSpeechService
from wolfbot.npc.tts import (
    InMemoryTtsCache,
    TtsProviderError,
    TtsRequest,
    TtsService,
)

log = logging.getLogger(__name__)


@dataclass
class NpcClientConfig:
    npc_id: str
    discord_bot_user_id: str
    persona_key: str
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
    # VC lifecycle callbacks. Master sends `seat_assigned` only to NPC bots
    # that were picked for an active game — at that moment we join VC; on
    # `seat_released` (or `npc_registered` arriving with no assignment) we
    # leave. Optional so tests / pure-message-loop scenarios can omit them.
    on_vc_join: Callable[[], Awaitable[None]] | None = None
    on_vc_leave: Callable[[], Awaitable[None]] | None = None
    # Self-mute hook: Master sends `set_mute_state` to flip the bot's own
    # voice self-mute (mic icon) so dead seats / non-discussion phases are
    # visually obvious. Optional so tests can omit it.
    on_set_mute: Callable[[bool], Awaitable[None]] | None = None
    # Self-post hook: when this NPC's TTS is authorized, post the spoken
    # text to the VC's attached chat from this bot's own account so the
    # speech is attributed (avatar + name) instead of being mirrored by
    # Master after the fact. Called once per authorized utterance.
    on_post_chat: Callable[[str], Awaitable[None]] | None = None

    _logic_cache: dict[str, LogicPacket] = field(default_factory=dict)
    _pending_playback: dict[str, _PendingForPlayback] = field(default_factory=dict)
    pending_authorizations: list[_AuthorizedPlayback] = field(default_factory=list)
    registered: bool = False
    assigned_seat: int | None = None
    assigned_game_id: str | None = None
    assigned_phase_id: str | None = None
    # Phase-D: per-(game_id) private state mirror. Master pushes via
    # PrivateStateSnapshot (full replace) at game start + NPC re-register,
    # then PrivateStateUpdate (append/patch) for incremental events. The
    # NPC bot uses this in vote / night / speech decision handlers.
    game_states: dict[str, NpcGameState] = field(default_factory=dict)
    # Phase-D: per-seat decision LLM. When None, vote / night handlers
    # fall through to the historical stub (target=None) — useful for
    # tests that don't want to bind a real LLM client.
    decision_llm: DecisionLLM | None = None

    # ---------------------------------------------------------- registration

    async def register(self, trace_id: str = "register") -> None:
        msg = NpcRegister(
            ts=self.now_ms(),
            trace_id=trace_id,
            npc_id=self.config.npc_id,
            discord_bot_user_id=self.config.discord_bot_user_id,
            persona_key=self.config.persona_key,
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
            await self._on_registered(NpcRegistered.model_validate(payload))
        elif t == "seat_assigned":
            await self._on_seat_assigned(SeatAssigned.model_validate(payload))
        elif t == "seat_released":
            await self._on_seat_released(SeatReleased.model_validate(payload))
        elif t == "set_mute_state":
            await self._on_set_mute_state(SetMuteState.model_validate(payload))
        elif t == "logic_packet":
            self._on_logic_packet(LogicPacket.model_validate(payload))
        elif t == "speak_request":
            await self._on_speak_request(SpeakRequest.model_validate(payload))
        elif t == "playback_authorized":
            await self._on_playback_authorized(PlaybackAuthorized.model_validate(payload))
        elif t == "playback_rejected":
            self._on_playback_rejected(PlaybackRejected.model_validate(payload))
        elif t == "private_state_snapshot":
            self._on_private_state_snapshot(
                PrivateStateSnapshot.model_validate(payload)
            )
        elif t == "private_state_update":
            self._on_private_state_update(
                PrivateStateUpdate.model_validate(payload)
            )
        elif t == "decide_vote_request":
            await self._on_decide_vote_request(
                DecideVoteRequest.model_validate(payload)
            )
        elif t == "decide_night_action_request":
            await self._on_decide_night_action_request(
                DecideNightActionRequest.model_validate(payload)
            )
        elif t == "wolf_chat_request":
            await self._on_wolf_chat_request(
                WolfChatRequest.model_validate(payload)
            )
        else:
            log.info("npc_client_unhandled_type type=%s", t)

    async def _on_registered(self, msg: NpcRegistered) -> None:
        self.registered = True
        self.assigned_seat = msg.assigned_seat
        self.assigned_game_id = msg.game_id
        self.assigned_phase_id = msg.phase_id
        log.info(
            "npc_registered npc_id=%s seat=%s game=%s",
            msg.npc_id,
            msg.assigned_seat,
            msg.game_id,
        )
        # Recovery path: if Master tells us we were already assigned at
        # register time (e.g. NPC bot reconnected mid-game), join VC.
        if msg.assigned_seat is not None and self.on_vc_join is not None:
            try:
                await self.on_vc_join()
            except Exception:
                log.exception(
                    "npc_vc_join_failed_on_recovery npc_id=%s", msg.npc_id
                )

    async def _on_seat_assigned(self, msg: SeatAssigned) -> None:
        self.assigned_seat = msg.seat_no
        self.assigned_game_id = msg.game_id
        self.assigned_phase_id = msg.phase_id
        log.info(
            "npc_seat_assigned npc_id=%s seat=%d game=%s",
            msg.npc_id,
            msg.seat_no,
            msg.game_id,
        )
        if self.on_vc_join is not None:
            try:
                await self.on_vc_join()
            except Exception:
                log.exception(
                    "npc_vc_join_failed npc_id=%s seat=%d",
                    msg.npc_id,
                    msg.seat_no,
                )

    async def _on_set_mute_state(self, msg: SetMuteState) -> None:
        if msg.npc_id != self.config.npc_id:
            return
        log.info(
            "npc_set_mute_state npc_id=%s self_mute=%s",
            msg.npc_id,
            msg.self_mute,
        )
        if self.on_set_mute is not None:
            try:
                await self.on_set_mute(msg.self_mute)
            except Exception:
                log.exception(
                    "npc_set_mute_failed npc_id=%s self_mute=%s",
                    msg.npc_id,
                    msg.self_mute,
                )

    async def _on_seat_released(self, msg: SeatReleased) -> None:
        log.info(
            "npc_seat_released npc_id=%s game=%s reason=%s",
            msg.npc_id,
            msg.game_id,
            msg.reason,
        )
        self.assigned_seat = None
        self.assigned_game_id = None
        self.assigned_phase_id = None
        # Drop per-game caches so a long-lived NPC process doesn't
        # accumulate `NpcGameState` + LogicPackets across every game it
        # plays. The state is push-replaced by Master at the next
        # PrivateStateSnapshot, so dropping it here only costs the next
        # game its first snapshot — already mandatory anyway. Guard for
        # the rare null game_id (older `SeatReleased` payloads carry it
        # as optional): without a key we can't target the right game, so
        # we skip the cleanup rather than risk wiping the wrong entry.
        if msg.game_id is not None:
            self.game_states.pop(msg.game_id, None)
            prefix = f"{msg.game_id}::"
            self._logic_cache = {
                pid: pkt
                for pid, pkt in self._logic_cache.items()
                if not pkt.phase_id.startswith(prefix)
            }
        if self.on_vc_leave is not None:
            try:
                await self.on_vc_leave()
            except Exception:
                log.exception("npc_vc_leave_failed npc_id=%s", msg.npc_id)

    def _on_logic_packet(self, packet: LogicPacket) -> None:
        self._logic_cache[packet.packet_id] = packet

    def _lookup_state_for_speech(
        self, request: SpeakRequest
    ) -> NpcGameState | None:
        """Find the matching `NpcGameState` for the speak request.

        SpeakRequest carries `phase_id` only; we recover the game id via
        the canonical ``{gid}::dayN::PHASE::seq`` format and look the
        state up in the in-memory mirror. Returns None when no snapshot
        has been received yet — the generator falls back to the
        SpeakRequest fields in that case.
        """
        from wolfbot.services.llm_trace import parse_game_id_from_phase_id

        gid = parse_game_id_from_phase_id(request.phase_id)
        if gid is None:
            return None
        return self.game_states.get(gid)

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
        # Phase-D: pass the per-game state mirror so the speech generator
        # can read its own role + private results + alive/dead lists from
        # the same source the vote/night handlers use, rather than from
        # the SpeakRequest's now-deprecated role/role_strategy/seat fields.
        state = self._lookup_state_for_speech(request)
        result = await self.speech.respond(
            logic=logic, request=request, now_ms=self.now_ms(), state=state,
        )
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
        # Post the spoken text to VC chat from this bot's own account
        # so the message is attributed to the speaking persona — not to
        # Master. Best-effort: a chat-post failure must not block the
        # voice playback that follows.
        if self.on_post_chat is not None:
            try:
                await self.on_post_chat(pending.text)
            except Exception:
                log.exception(
                    "npc_post_chat_failed request=%s",
                    auth.request_id,
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

    # ---------------------------------------------------------- phase-D state

    def _on_private_state_snapshot(self, snapshot: PrivateStateSnapshot) -> None:
        """Replace the per-game state for ``snapshot.game_id`` wholesale.

        Targeted at this NPC only — Master sends one snapshot per NPC at
        game start and on re-register. Receiving one for a different
        ``npc_id`` is a routing bug; we log and drop.
        """
        if snapshot.npc_id != self.config.npc_id:
            log.warning(
                "npc_private_state_snapshot_misrouted self=%s got=%s",
                self.config.npc_id, snapshot.npc_id,
            )
            return
        self.game_states[snapshot.game_id] = state_from_snapshot(snapshot)
        log.info(
            "npc_private_state_snapshot game=%s seat=%d role=%s persona=%s "
            "alive=%d wolf_partners=%d seer=%d medium=%d guards=%d wolf_chat=%d",
            snapshot.game_id,
            snapshot.seat_no,
            snapshot.role,
            snapshot.persona_key,
            len(snapshot.alive_seats),
            len(snapshot.partner_wolves),
            len(snapshot.seer_results),
            len(snapshot.medium_results),
            len(snapshot.guard_history),
            len(snapshot.wolf_chat_history),
        )

    def _on_private_state_update(self, update: PrivateStateUpdate) -> None:
        """Mutate the per-game state for ``update.game_id`` in place.

        A missing snapshot for that game is treated as a Master-side bug
        (snapshot must precede every update). We log and drop rather than
        synthesize state from incomplete information.
        """
        if update.npc_id != self.config.npc_id:
            log.warning(
                "npc_private_state_update_misrouted self=%s got=%s",
                self.config.npc_id, update.npc_id,
            )
            return
        state = self.game_states.get(update.game_id)
        if state is None:
            log.warning(
                "npc_private_state_update_no_snapshot game=%s kind=%s",
                update.game_id, update.update_kind,
            )
            return
        apply_update(state, update)
        log.info(
            "npc_private_state_update game=%s kind=%s",
            update.game_id, update.update_kind,
        )

    async def _on_decide_vote_request(self, req: DecideVoteRequest) -> None:
        """Phase-D: NPC decides its own vote target via NPC_LLM.

        Falls back to ``target_seat=None`` (abstain) when state is
        missing, no decision LLM is configured, or the LLM call /
        response parse fails. Every fallback is logged so the viewer
        surfaces the seat as silent for that round (per the user's
        "log it so the viewer shows the seat went silent" rule).
        """
        if req.npc_id != self.config.npc_id:
            return
        target, reason = await self._decide_vote_target(req)
        decision = VoteDecision(
            ts=self.now_ms(),
            trace_id=req.trace_id,
            request_id=req.request_id,
            npc_id=self.config.npc_id,
            seat_no=req.seat_no,
            target_seat=target,
            reason_summary=reason,
        )
        await self.send(decision.model_dump_json())

    async def _decide_vote_target(
        self, req: DecideVoteRequest
    ) -> tuple[int | None, str]:
        from wolfbot.npc.personas import NPC_PERSONAS_BY_KEY

        state = self.game_states.get(req.game_id)
        if state is None:
            log.warning(
                "npc_vote_no_state game=%s seat=%d", req.game_id, req.seat_no,
            )
            return None, "no_state"
        if self.decision_llm is None:
            return None, "no_decision_llm"
        persona = NPC_PERSONAS_BY_KEY.get(state.persona_key)
        if persona is None:
            log.warning(
                "npc_vote_unknown_persona persona=%s", state.persona_key,
            )
            return None, "unknown_persona"
        legal = frozenset(seat for seat, _name in req.candidate_seats)
        system, user = build_vote_prompt(state=state, persona=persona, request=req)
        # Mirror `_decide_night_target`: track the LLM call's success
        # uniformly so the abstain fallback below covers transport errors
        # (504 DEADLINE_EXCEEDED, 429 RESOURCE_EXHAUSTED, ...) the same
        # way it covers parse failures. Without this, a Gemini 504 on
        # the vote turn drops the ballot to abstain, weakening the
        # village's ability to lynch a wolf when one round-trip flakes.
        result_target_seat: int | None = None
        reason_summary = "llm_error"
        try:
            raw = await self.decision_llm.decide_json(
                system_prompt=system, user_prompt=user, schema=_VOTE_SCHEMA,
            )
            result = parse_decision(raw, legal_seats=legal)
            result_target_seat = result.target_seat
            reason_summary = result.reason_summary
        except Exception:
            log.exception(
                "npc_vote_llm_failed game=%s seat=%d", req.game_id, req.seat_no,
            )
        # Forbid abstention in voting. The schema disallows null and the
        # prompt explicitly says "棄権禁止", but if the model still drops
        # back (parse error, out-of-set target, persona inertia, or LLM
        # transport error) we pick a deterministic-but-uniform fallback
        # so the seat doesn't end up in the silent-abstain bucket.
        if result_target_seat is None and legal:
            rng = random.Random(
                f"{req.game_id}:{req.seat_no}:{req.round_}".__hash__()
            )
            fallback = rng.choice(sorted(legal))
            log.info(
                "npc_vote_abstain_fallback game=%s seat=%d -> %d reason=%s",
                req.game_id, req.seat_no, fallback,
                reason_summary or "(none)",
            )
            return fallback, f"abstain_fallback:{reason_summary or ''}"
        return result_target_seat, reason_summary

    async def _on_decide_night_action_request(
        self, req: DecideNightActionRequest
    ) -> None:
        """Phase-D: NPC decides its own night action via NPC_LLM.

        Same fallback shape as the vote handler — None target on missing
        state, no decision LLM, LLM error, or out-of-set response.
        """
        if req.npc_id != self.config.npc_id:
            return
        target, reason = await self._decide_night_target(req)
        decision = NightActionDecision(
            ts=self.now_ms(),
            trace_id=req.trace_id,
            request_id=req.request_id,
            npc_id=self.config.npc_id,
            seat_no=req.seat_no,
            action_kind=req.action_kind,
            target_seat=target,
            reason_summary=reason,
        )
        await self.send(decision.model_dump_json())

    async def _on_wolf_chat_request(self, req: WolfChatRequest) -> None:
        """Phase-D: wolf NPC posts a coordination line.

        Drops with empty text on missing state / non-wolf role / no
        decision LLM / LLM error / parse failure. Master's broker still
        runs on every WolfChatSend, so an empty text is silently
        absorbed rather than persisted.
        """
        if req.npc_id != self.config.npc_id:
            return
        text = await self._build_wolf_chat_line(req)
        decision = WolfChatSend(
            ts=self.now_ms(),
            trace_id=req.trace_id,
            npc_id=self.config.npc_id,
            seat_no=req.seat_no,
            game_id=req.game_id,
            text=text or "",
            request_id=req.request_id,
        )
        await self.send(decision.model_dump_json())

    async def _build_wolf_chat_line(self, req: WolfChatRequest) -> str | None:
        from wolfbot.npc.personas import NPC_PERSONAS_BY_KEY

        state = self.game_states.get(req.game_id)
        if state is None:
            log.warning(
                "npc_wolf_chat_no_state game=%s seat=%d",
                req.game_id, req.seat_no,
            )
            return None
        if state.role != "WEREWOLF":
            log.info(
                "npc_wolf_chat_drop_non_wolf seat=%d role=%s",
                req.seat_no, state.role,
            )
            return None
        if self.decision_llm is None:
            return None
        persona = NPC_PERSONAS_BY_KEY.get(state.persona_key)
        if persona is None:
            return None
        system, user = build_wolf_chat_prompt(
            state=state,
            persona=persona,
            candidates=req.candidate_seats,
            public_state_summary=req.public_state_summary,
        )
        try:
            raw = await self.decision_llm.decide_json(
                system_prompt=system, user_prompt=user, schema=_WOLF_CHAT_SCHEMA,
            )
        except Exception:
            log.exception(
                "npc_wolf_chat_llm_failed game=%s seat=%d",
                req.game_id, req.seat_no,
            )
            return None
        return parse_wolf_chat_text(raw)

    async def _decide_night_target(
        self, req: DecideNightActionRequest
    ) -> tuple[int | None, str]:
        from wolfbot.npc.personas import NPC_PERSONAS_BY_KEY

        state = self.game_states.get(req.game_id)
        if state is None:
            log.warning(
                "npc_night_no_state game=%s seat=%d", req.game_id, req.seat_no,
            )
            return None, "no_state"
        if self.decision_llm is None:
            return None, "no_decision_llm"
        persona = NPC_PERSONAS_BY_KEY.get(state.persona_key)
        if persona is None:
            return None, "unknown_persona"
        legal = frozenset(seat for seat, _name in req.candidate_seats)
        system, user = build_night_prompt(state=state, persona=persona, request=req)
        # Track parse + transport errors uniformly so the abstain
        # fallback below covers BOTH cases. Game ``06c38cd43494``
        # NIGHT_1 stalled because Vertex AI returned 504
        # DEADLINE_EXCEEDED on the seer's divine call — the previous
        # version of this method short-circuited with `return None,
        # "llm_error"` on any exception, bypassing the random-legal
        # fallback. The deadline closed with `pending_decisions
        # missing_seats=[1]` and the game parked in
        # WAITING_HOST_DECISION.
        result_target_seat: int | None = None
        reason_summary = "llm_error"
        try:
            raw = await self.decision_llm.decide_json(
                system_prompt=system, user_prompt=user, schema=_NIGHT_SCHEMA,
            )
            result = parse_decision(raw, legal_seats=legal)
            result_target_seat = result.target_seat
            reason_summary = result.reason_summary
        except Exception:
            log.exception(
                "npc_night_llm_failed game=%s seat=%d kind=%s",
                req.game_id, req.seat_no, req.action_kind,
            )
        # Forbid skipping for night actions. Master rejects target=None
        # with ILLEGAL_TARGET; the missing seat then deadlocks the
        # NIGHT phase via pending_decisions until the host force-skips.
        # Live game stuck on day-1 NIGHT because the knight returned
        # null saying "GJ リスク回避し次夜余地残す". Force a legal pick
        # so the phase always advances; persona keeps a chance to do
        # 捨て護衛 / 価値の薄い位置 via the LLM choice itself.
        if result_target_seat is None and legal:
            rng = random.Random(
                f"{req.game_id}:{req.seat_no}:{req.action_kind}".__hash__()
            )
            fallback = rng.choice(sorted(legal))
            log.info(
                "npc_night_abstain_fallback game=%s seat=%d kind=%s -> %d "
                "reason=%s",
                req.game_id, req.seat_no, req.action_kind, fallback,
                reason_summary or "(none)",
            )
            return fallback, f"abstain_fallback:{reason_summary or ''}"
        return result_target_seat, reason_summary


__all__ = ["NpcClient", "NpcClientConfig"]


# Force imports referenced for typing extensions.
_ = (asyncio,)
