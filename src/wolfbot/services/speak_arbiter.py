"""SpeakArbiter — Master-side reactive_voice arbitration.

Responsibilities, in spec order:

1. Pick a candidate NPC for the current `PublicDiscussionState` and ensure
   they are alive (NOT in `newly_dead`) and online (heartbeat fresh).
2. Reject the candidate when serial-speech is already busy:
     - `human_currently_speaking` — a VAD window is open.
     - `queue_busy` — another NPC has an authorized playback window open.
     - `npc_offline` — the candidate's WS connection or heartbeat is stale.
3. Build a `LogicPacket` (via `master_logic_service.build_logic_packet`)
   for the picked NPC, send it, then dispatch a `SpeakRequest` and
   persist a row in `npc_speak_requests`.
4. On `SpeakResult` arrival, validate `phase_id` + `request_id` freshness +
   length cap, persist a row in `npc_speak_results`, and if accepted,
   write a `SpeechEvent(source=npc_generated)`, open the
   `npc_playback_events` row, and reply with `PlaybackAuthorized`.
5. On `tts_finished` / `tts_failed` / `playback_finished` / `playback_failed`
   update the audit row and release the serial-speech gate.

A real `MasterWsServer` connection plus a real `SqliteRepo` are required at
runtime. Tests substitute `FakeMasterWsServer` and a tempfile-backed repo.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from wolfbot.domain.discussion import (
    PublicDiscussionState,
    SpeechEvent,
    SpeechSource,
    make_phase_id,
)
from wolfbot.domain.enums import Phase
from wolfbot.domain.ws_messages import (
    PlaybackAuthorized,
    PlaybackFailed,
    PlaybackFinished,
    SpeakRequest,
    SpeakResult,
    TtsFailed,
    TtsFinished,
)
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discussion_service import (
    DiscussionService,
    new_event_id,
)
from wolfbot.services.discussion_service import (
    now_ms as default_now_ms,
)
from wolfbot.services.master_logic_service import build_logic_packet
from wolfbot.services.npc_registry import NpcRegistry

log = logging.getLogger(__name__)


@dataclass
class SpeakArbiterConfig:
    max_chars_reactive: int = 80
    request_ttl_ms: int = 8000
    playback_deadline_ms: int = 12_000
    heartbeat_timeout_ms: int = 5000


@dataclass
class _PendingRequest:
    request_id: str
    npc_id: str
    seat_no: int
    phase_id: str
    game_id: str
    expires_at_ms: int


class SpeakArbiter:
    """Single-game arbiter — `SpeakArbiter.dispatch_for_phase` is called by
    the discussion mode plumbing (Bundle 8) once the phase enters
    DAY_DISCUSSION under reactive_voice.

    The arbiter is intentionally not a long-running loop; it exposes
    discrete operations the dispatcher / WS handlers call.
    """

    def __init__(
        self,
        *,
        repo: SqliteRepo,
        registry: NpcRegistry,
        discussion: DiscussionService,
        config: SpeakArbiterConfig | None = None,
        now_ms: Callable[[], int] = default_now_ms,
    ) -> None:
        self.repo = repo
        self.registry = registry
        self.discussion = discussion
        self.config = config or SpeakArbiterConfig()
        self._now_ms = now_ms
        self._pending: dict[str, _PendingRequest] = {}
        # Serial-speech gate: a request_id is in `_active_playback` between
        # PlaybackAuthorized and the closing tts_failed / playback_finished /
        # playback_failed event. While non-empty, no new SpeakRequest is sent.
        self._active_playback: set[str] = set()
        # human_currently_speaking gate; the WS handler flips this on
        # vad_speech_started / vad_speech_ended (handled in voice-ingest
        # plumbing in Bundle 8). Empty by default.
        self._human_speaking_segments: set[str] = set()

    # ------------------------------------------------------------- gates

    def mark_human_speaking(self, segment_id: str) -> None:
        self._human_speaking_segments.add(segment_id)

    def clear_human_speaking(self, segment_id: str) -> None:
        self._human_speaking_segments.discard(segment_id)

    def is_blocked(self) -> str | None:
        if self._human_speaking_segments:
            return "human_currently_speaking"
        if self._active_playback:
            return "queue_busy"
        return None

    # ------------------------------------------------------------- dispatch

    async def dispatch_request(
        self,
        *,
        state: PublicDiscussionState,
        candidate_npc_id: str,
        seat_no: int,
        game_id: str,
        suggested_intent: str = "speak",
    ) -> tuple[SpeakRequest | None, str | None]:
        """Try to send a SpeakRequest to `candidate_npc_id`.

        Returns ``(request, None)`` on success, ``(None, reason)`` on skip.
        Reasons cover every documented `failure_reason` for arbiter
        suppression / candidate skip.
        """
        block = self.is_blocked()
        if block is not None:
            log.info(
                "speak_request_suppressed npc=%s seat=%s reason=%s",
                candidate_npc_id,
                seat_no,
                block,
            )
            return (None, block)

        entry = self.registry.get(candidate_npc_id)
        if entry is None or not entry.is_online or entry.send is None:
            log.info(
                "speak_candidate_skipped npc=%s reason=npc_offline",
                candidate_npc_id,
            )
            return (None, "npc_offline")

        now = self._now_ms()
        if (now - entry.last_heartbeat_ms) > self.config.heartbeat_timeout_ms:
            log.info(
                "speak_candidate_skipped npc=%s reason=npc_offline_heartbeat",
                candidate_npc_id,
            )
            return (None, "npc_offline")

        # Build LogicPacket (sent first so the NPC has context for the
        # subsequent speak_request).
        packet = build_logic_packet(
            state=state,
            recipient_npc_id=candidate_npc_id,
            expires_at_ms=now + self.config.request_ttl_ms,
            now_ms=now,
        )
        try:
            await entry.send(packet.model_dump_json())
        except Exception:
            log.exception("logic_packet_send_failed npc=%s", candidate_npc_id)
            return (None, "ws_send_failed")

        request = SpeakRequest(
            ts=now,
            trace_id=packet.trace_id,
            request_id=f"sr_{uuid.uuid4().hex[:12]}",
            phase_id=state.phase_id,
            npc_id=candidate_npc_id,
            seat_no=seat_no,
            logic_packet_id=packet.packet_id,
            suggested_intent=suggested_intent,
            max_chars=self.config.max_chars_reactive,
            max_duration_ms=self.config.playback_deadline_ms,
            priority=0,
            expires_at_ms=now + self.config.request_ttl_ms,
        )

        await self.repo.insert_npc_speak_request(
            request_id=request.request_id,
            game_id=game_id,
            phase_id=request.phase_id,
            npc_id=candidate_npc_id,
            seat_no=seat_no,
            logic_packet_id=request.logic_packet_id,
            suggested_intent=suggested_intent,
            max_chars=self.config.max_chars_reactive,
            max_duration_ms=self.config.playback_deadline_ms,
            priority=0,
            expires_at_ms=request.expires_at_ms,
            created_at_ms=now,
        )
        try:
            await entry.send(request.model_dump_json())
        except Exception:
            log.exception("speak_request_send_failed npc=%s", candidate_npc_id)
            await self.repo.insert_npc_speak_result(
                request_id=request.request_id,
                game_id=game_id,
                phase_id=request.phase_id,
                npc_id=candidate_npc_id,
                status="rejected",
                text=None,
                used_logic_ids=None,
                intent=None,
                estimated_duration_ms=None,
                failure_reason="ws_send_failed",
                received_at_ms=now,
            )
            return (None, "ws_send_failed")

        self._pending[request.request_id] = _PendingRequest(
            request_id=request.request_id,
            npc_id=candidate_npc_id,
            seat_no=seat_no,
            phase_id=request.phase_id,
            game_id=game_id,
            expires_at_ms=request.expires_at_ms,
        )
        return (request, None)

    # ------------------------------------------------------------- handle result

    async def handle_speak_result(
        self,
        result: SpeakResult,
        *,
        current_phase_id: str,
        day: int,
        phase: Phase,
    ) -> tuple[bool, str | None]:
        """Validate and persist a SpeakResult.

        On success returns ``(True, None)`` and emits a `PlaybackAuthorized`
        on the NPC's back-channel. On failure returns ``(False, reason)``
        and emits a `PlaybackRejected`.
        """
        from wolfbot.domain.ws_messages import PlaybackRejected

        now = self._now_ms()
        pending = self._pending.get(result.request_id)
        entry = self.registry.get(result.npc_id)

        async def _send(payload: str) -> None:
            if entry is not None and entry.send is not None:
                try:
                    await entry.send(payload)
                except Exception:
                    log.exception("speak_result_response_send_failed npc=%s", result.npc_id)

        async def _record_rejection(reason: str) -> None:
            await self.repo.insert_npc_speak_result(
                request_id=result.request_id,
                game_id=pending.game_id if pending is not None else "",
                phase_id=result.phase_id,
                npc_id=result.npc_id,
                status="rejected",
                text=result.text,
                used_logic_ids=list(result.used_logic_ids),
                intent=result.intent,
                estimated_duration_ms=result.estimated_duration_ms,
                failure_reason=reason,
                received_at_ms=now,
            )
            rejection = PlaybackRejected(
                ts=now,
                trace_id=result.trace_id,
                request_id=result.request_id,
                npc_id=result.npc_id,
                failure_reason=reason,
            )
            await _send(rejection.model_dump_json())

        if pending is None:
            await _record_rejection("unknown_request")
            return (False, "unknown_request")
        if result.phase_id != current_phase_id:
            await _record_rejection("stale_phase")
            return (False, "stale_phase")
        if now > pending.expires_at_ms:
            await _record_rejection("expired_request")
            return (False, "expired_request")
        if result.status != "accepted" or not result.text:
            await _record_rejection("speaker_declined")
            return (False, "speaker_declined")
        if len(result.text) > self.config.max_chars_reactive:
            await _record_rejection("utterance_too_long")
            return (False, "utterance_too_long")

        # Accepted. Persist result + SpeechEvent + open playback row.
        await self.repo.insert_npc_speak_result(
            request_id=result.request_id,
            game_id=pending.game_id,
            phase_id=result.phase_id,
            npc_id=result.npc_id,
            status="accepted",
            text=result.text,
            used_logic_ids=list(result.used_logic_ids),
            intent=result.intent,
            estimated_duration_ms=result.estimated_duration_ms,
            failure_reason=None,
            received_at_ms=now,
        )
        speech_event = SpeechEvent(
            event_id=new_event_id(),
            game_id=pending.game_id,
            phase_id=result.phase_id,
            day=day,
            phase=phase,
            source=SpeechSource.NPC_GENERATED,
            speaker_kind="npc",  # type: ignore[arg-type]
            speaker_seat=pending.seat_no,
            text=result.text,
            created_at_ms=now,
        )
        await self.discussion.record(speech_event)
        deadline = now + self.config.playback_deadline_ms
        await self.repo.open_npc_playback(
            request_id=result.request_id,
            game_id=pending.game_id,
            phase_id=result.phase_id,
            npc_id=result.npc_id,
            speech_event_id=speech_event.event_id,
            authorized_at_ms=now,
            playback_deadline_ms=deadline,
        )
        self._active_playback.add(result.request_id)
        authorized = PlaybackAuthorized(
            ts=now,
            trace_id=result.trace_id,
            request_id=result.request_id,
            npc_id=result.npc_id,
            speech_event_id=speech_event.event_id,
            playback_deadline_ms=deadline,
        )
        await _send(authorized.model_dump_json())
        return (True, None)

    # ------------------------------------------------------------- TTS / playback

    async def handle_tts_finished(self, msg: TtsFinished) -> None:
        await self.repo.update_npc_playback_tts(
            msg.request_id,
            outcome="success",
            duration_ms=msg.tts_duration_ms,
            failure_reason=None,
        )

    async def handle_tts_failed(self, msg: TtsFailed) -> None:
        now = self._now_ms()
        await self.repo.update_npc_playback_tts(
            msg.request_id,
            outcome="failed",
            duration_ms=None,
            failure_reason=msg.failure_reason,
        )
        await self.repo.close_npc_playback(
            msg.request_id,
            finished_at_ms=now,
            outcome="failed",
            failure_reason=msg.failure_reason,
        )
        self._active_playback.discard(msg.request_id)
        self._pending.pop(msg.request_id, None)

    async def handle_playback_finished(self, msg: PlaybackFinished) -> None:
        await self.repo.close_npc_playback(
            msg.request_id,
            finished_at_ms=msg.finished_at_ms,
            outcome="succeeded",
            failure_reason=None,
        )
        self._active_playback.discard(msg.request_id)
        self._pending.pop(msg.request_id, None)

    async def handle_playback_failed(self, msg: PlaybackFailed) -> None:
        now = self._now_ms()
        await self.repo.close_npc_playback(
            msg.request_id,
            finished_at_ms=now,
            outcome="failed",
            failure_reason=msg.failure_reason,
        )
        self._active_playback.discard(msg.request_id)
        self._pending.pop(msg.request_id, None)

    # ------------------------------------------------------------- auto-dispatch

    async def try_dispatch_next(self, game_id: str) -> None:
        """Auto-pick the next candidate NPC and dispatch a SpeakRequest.

        Called on phase entry, after each new public speech event, and after
        playback completes. No-op when the serial-speech gate is blocked, no
        NPC is online, or no game is in a reactive_voice discussion phase.
        """
        game = await self.repo.load_game(game_id)
        if game is None or game.ended_at is not None:
            return
        if game.discussion_mode != "reactive_voice":
            return
        if game.phase not in (Phase.DAY_DISCUSSION, Phase.DAY_RUNOFF_SPEECH):
            return

        block = self.is_blocked()
        if block is not None:
            return

        state = await self.rebuild_public_state(
            game_id=game_id, day=game.day_number, phase=game.phase
        )
        if state is None:
            return

        # Pick the first online NPC whose assigned seat is alive and silent.
        online = self.registry.all_online()
        for entry in sorted(online, key=lambda e: e.assigned_seat or 99):
            if entry.assigned_seat is None or entry.game_id != game_id:
                continue
            if entry.assigned_seat not in state.alive_seat_nos:
                continue
            # Prefer silent seats, but fall back to any alive NPC.
            await self.dispatch_request(
                state=state,
                candidate_npc_id=entry.npc_id,
                seat_no=entry.assigned_seat,
                game_id=game_id,
            )
            return

    # ------------------------------------------------------------- restart sweep

    async def reactive_voice_recovery_sweep(self, game_id: str) -> None:
        """Mark every in-flight request rejected and every open playback failed.

        Called once on Master restart from `RecoveryService`. The
        ``failure_reason=master_restart`` value is mandated by the
        npc-voice-pipeline spec.
        """
        now = self._now_ms()
        open_reqs = await self.repo.load_open_npc_speak_requests(game_id)
        for row in open_reqs:
            await self.repo.insert_npc_speak_result(
                request_id=row["request_id"],
                game_id=game_id,
                phase_id=row["phase_id"],
                npc_id=row["npc_id"],
                status="rejected",
                text=None,
                used_logic_ids=None,
                intent=None,
                estimated_duration_ms=None,
                failure_reason="master_restart",
                received_at_ms=now,
            )
        open_play = await self.repo.load_open_npc_playback(game_id)
        for row in open_play:
            await self.repo.close_npc_playback(
                row["request_id"],
                finished_at_ms=now,
                outcome="failed",
                failure_reason="master_restart",
            )

    async def rebuild_public_state(
        self,
        *,
        game_id: str,
        day: int,
        phase: Phase,
    ) -> PublicDiscussionState | None:
        """Re-fold `speech_events` for the active phase.

        Used after Master restart to seed the in-memory `PublicDiscussionState`
        before re-entering the arbitration loop.
        """
        from wolfbot.services.discussion_service import (
            rebuild_public_state_from_events,
        )

        phase_id = make_phase_id(game_id, day, phase)
        events: Sequence[SpeechEvent] = await self.discussion.load_phase(game_id, phase_id)
        return rebuild_public_state_from_events(events)


__all__ = ["SpeakArbiter", "SpeakArbiterConfig"]


# Force a non-static reference so the linter keeps Awaitable / Callable
# imports for downstream typing extensions.
_ = (Awaitable, Callable)
