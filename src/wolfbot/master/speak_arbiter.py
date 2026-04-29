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

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from wolfbot.domain.discussion import (
    PublicDiscussionState,
    SpeechEvent,
    SpeechSource,
    make_phase_id,
)
from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Seat
from wolfbot.domain.rules import compute_vote_result
from wolfbot.domain.ws_messages import (
    PlaybackAuthorized,
    PlaybackFailed,
    PlaybackFinished,
    RecentSpeech,
    SpeakRequest,
    SpeakResult,
    TtsFailed,
    TtsFinished,
)
from wolfbot.llm.prompt_builder import build_strategy_block
from wolfbot.master.logic_service import build_logic_packet
from wolfbot.master.npc_registry import NpcRegistry
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discussion_service import (
    DiscussionService,
    new_event_id,
)
from wolfbot.services.discussion_service import (
    now_ms as default_now_ms,
)

log = logging.getLogger(__name__)


# Last N speeches included in the LogicPacket. Mirrors the rounds-mode
# prompt builder which surfaces the trailing 40 PLAYER_SPEECH log lines;
# 20 is plenty for an 80-char reactive reply and keeps WS frames small.
_RECENT_SPEECH_CAP = 20

# Diversity guards on top of the addressed / silent / LRU sort key.
# - `_PAIR_VOLLEY_WINDOW`: when the last N events come from exactly 2
#   distinct seats AND none carried structured "info" (currently a
#   ``co_declaration``), demote both seats so a third NPC gets to speak.
#   This breaks the ラキオ ↔ ジョナス ping-pong: each pair gets to volley
#   the full window before being told to step aside.
# - `_CONSECUTIVE_CAP`: same seat speaking ≥ N times in a row is also
#   demoted. Mostly fires when a human keeps re-addressing the same NPC
#   or when a buggy upstream tries to dispatch the same seat twice.
_PAIR_VOLLEY_WINDOW = 4
_CONSECUTIVE_CAP = 3


def _parse_day_from_phase_id(phase_id: str) -> int | None:
    """Extract the integer day from a canonical phase_id token.

    Phase ids look like ``gid::day3::DAY_RUNOFF_SPEECH::1``; we walk the
    ``::`` segments and look for one matching ``dayN``. Returns ``None``
    when the format doesn't match — the caller treats that as "skip,
    don't mark done" rather than crashing the WS handler.
    """
    for token in phase_id.split("::"):
        if token.startswith("day"):
            tail = token[3:]
            if tail.isdigit():
                return int(tail)
    return None


def _compute_demoted_seats(
    summary: Sequence[tuple[int, bool]],
) -> frozenset[int]:
    """Return seats that should be demoted in the next pick.

    Two independent gates, OR'd together:

    1. Last ``_PAIR_VOLLEY_WINDOW`` events came from exactly 2 distinct
       seats AND none of them flagged ``has_info`` (= no CO declared) →
       both seats demoted.
    2. Last ``_CONSECUTIVE_CAP`` events all came from a single seat →
       that seat demoted.

    Returns an empty set when the window is too short or no gate fires.
    """
    demoted: set[int] = set()
    if len(summary) >= _PAIR_VOLLEY_WINDOW:
        window = list(summary)[-_PAIR_VOLLEY_WINDOW:]
        seats_in_window = {seat for seat, _ in window}
        any_info = any(has_info for _, has_info in window)
        if len(seats_in_window) == 2 and not any_info:
            demoted |= seats_in_window
    if len(summary) >= _CONSECUTIVE_CAP:
        tail = list(summary)[-_CONSECUTIVE_CAP:]
        if len({seat for seat, _ in tail}) == 1:
            demoted.add(tail[0][0])
    return frozenset(demoted)


@dataclass
class SpeakArbiterConfig:
    # Per-utterance hard cap. Was 80 originally — too tight: the LLM
    # routinely hit the limit mid-sentence (e.g. ending with 「それが気」
    # when about to say 「気になって」), and VOICEVOX then read the
    # truncated fragment aloud. 140 leaves room for a complete thought
    # while keeping playback under the 12s deadline (≈8s of audio at
    # VOICEVOX's default speed). The system prompt also instructs the
    # model to finish a sentence even if it has to be shorter than this
    # cap, so 140 is the ceiling, not the target length.
    max_chars_reactive: int = 140
    request_ttl_ms: int = 8000
    playback_deadline_ms: int = 12_000
    heartbeat_timeout_ms: int = 5000
    vad_finalization_timeout_ms: int = 4000


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
        runoff_announce: Callable[[Seat], Awaitable[None]] | None = None,
        runoff_wake: Callable[[str], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.repo = repo
        self.registry = registry
        self.discussion = discussion
        self.config = config or SpeakArbiterConfig()
        self._now_ms = now_ms
        # Seedable RNG for the picker tiebreak. Production uses an
        # un-seeded `random.Random()`; tests inject a deterministic one.
        self._rng = rng if rng is not None else random.Random()
        # Optional Master-narration hook fired right before dispatching a
        # tied LLM candidate's SpeakRequest in DAY_RUNOFF_SPEECH so Levi
        # can name the speaker ("続いて、X 様の最終演説でございます。").
        # The arbiter awaits the callback so the announcement plays
        # before the NPC's TTS so they don't overlap in VC.
        self._runoff_announce = runoff_announce
        # Optional engine-wake hook called when every tied LLM candidate
        # has finished or been marked done; lets DAY_RUNOFF_SPEECH advance
        # immediately to DAY_RUNOFF instead of waiting for the deadline.
        self._runoff_wake = runoff_wake
        # Strong refs to per-runoff watchdog tasks so they don't get
        # garbage-collected mid-sleep.
        self._runoff_watchdog_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[str, _PendingRequest] = {}
        # Serial-speech gate: a request_id is in `_active_playback` between
        # PlaybackAuthorized and the closing tts_failed / playback_finished /
        # playback_failed event. While non-empty, no new SpeakRequest is sent.
        self._active_playback: set[str] = set()
        # Playback deadline tracking: request_id → deadline_ms.
        self._playback_deadlines: dict[str, int] = {}
        # human_currently_speaking gate; the WS handler flips this on
        # vad_speech_started / vad_speech_ended (handled in voice-ingest
        # plumbing in Bundle 8). Empty by default.
        self._human_speaking_segments: set[str] = set()
        # Segments awaiting STT finalization. The human-speaking gate stays
        # closed for a segment until speech_event_payload or stt_failed
        # arrives (or vad_finalization_timeout_ms elapses).
        # segment_id → deadline_ms
        self._pending_stt_segments: dict[str, int] = {}

    # ------------------------------------------------------------- gates

    def mark_human_speaking(self, segment_id: str) -> None:
        self._human_speaking_segments.add(segment_id)

    def mark_pending_stt(self, segment_id: str) -> None:
        """VAD ended — keep gate held until STT finalizes or times out."""
        deadline = self._now_ms() + self.config.vad_finalization_timeout_ms
        self._pending_stt_segments[segment_id] = deadline

    def finalize_stt(self, segment_id: str) -> None:
        """STT completed (payload or failure) — release the segment gate."""
        self._pending_stt_segments.pop(segment_id, None)
        self._human_speaking_segments.discard(segment_id)

    def clear_human_speaking(self, segment_id: str) -> None:
        self._human_speaking_segments.discard(segment_id)

    def is_blocked(self) -> str | None:
        now = self._now_ms()
        # Sweep expired STT finalization deadlines — release segments whose
        # STT never arrived within vad_finalization_timeout_ms.
        expired_stt = [
            sid for sid, dl in self._pending_stt_segments.items() if now > dl
        ]
        for sid in expired_stt:
            log.info("stt_finalization_timeout segment=%s", sid)
            self._pending_stt_segments.pop(sid, None)
            self._human_speaking_segments.discard(sid)
        if self._human_speaking_segments:
            return "human_currently_speaking"
        # Sweep expired playback deadlines — close rows whose NPC never
        # reported tts_failed / playback_finished / playback_failed.
        expired_pb = [
            rid for rid, dl in self._playback_deadlines.items() if now > dl
        ]
        for rid in expired_pb:
            log.info("playback_deadline_exceeded request=%s", rid)
            self._active_playback.discard(rid)
            self._playback_deadlines.pop(rid, None)
            self._pending.pop(rid, None)
            # DB close is best-effort — fire-and-forget in the sync check.
            # The actual row closure is done in _sweep_expired_playback.
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
        selection_reason: str | None = None,
        public_state_snapshot: dict[str, Any] | None = None,
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

        # Resolve seat data: recent_speeches (with display names attached),
        # alive/dead seat lists, and the candidate's role+strategy. All
        # best-effort — if a load fails we fall back to empty/null and
        # still dispatch (the prompt degrades gracefully to the historical
        # minimal shape).
        recent_speeches, alive_seats, dead_seats, role_name, role_strategy = (
            await self._collect_request_context(state, seat_no)
        )
        past_votes = await self._load_past_votes(game_id, state.day)

        # Build LogicPacket (sent first so the NPC has context for the
        # subsequent speak_request).
        packet = build_logic_packet(
            state=state,
            recipient_npc_id=candidate_npc_id,
            expires_at_ms=now + self.config.request_ttl_ms,
            now_ms=now,
            recent_speeches=recent_speeches,
            past_votes=past_votes,
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
            role=role_name,
            role_strategy=role_strategy,
            alive_seats=alive_seats,
            dead_seats=dead_seats,
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
            selection_reason=selection_reason,
            public_state_snapshot=public_state_snapshot,
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

    # --------------------------------------------------- request context loader

    async def _collect_request_context(
        self,
        state: PublicDiscussionState,
        seat_no: int,
    ) -> tuple[
        tuple[RecentSpeech, ...],
        tuple[tuple[int, str], ...],
        tuple[tuple[int, str], ...],
        str | None,
        str | None,
    ]:
        """Load the data the NPC prompt needs but the arbiter doesn't already have.

        Returns ``(recent_speeches, alive_seats, dead_seats, role_name,
        role_strategy)``. Each piece is independently best-effort: a failed
        DB read for one slot logs and falls back to an empty value while
        the others still populate. The intent is that a transient repo
        glitch must NOT block dispatching — the NPC then sees the older
        minimal prompt shape rather than no prompt at all.
        """
        recent: tuple[RecentSpeech, ...] = ()
        try:
            events = await self.discussion.load_phase(state.game_id, state.phase_id)
            seats = await self.repo.load_seats(state.game_id)
            seat_name_by_no = {s.seat_no: s.display_name for s in seats}
            recent_list: list[RecentSpeech] = []
            for ev in events:
                if ev.source == SpeechSource.PHASE_BASELINE:
                    continue
                if ev.speaker_seat is None or not ev.text:
                    continue
                name = seat_name_by_no.get(ev.speaker_seat, f"席{ev.speaker_seat}")
                # Trim very long speeches; the NPC only needs the gist.
                snippet = ev.text.strip().replace("\n", " ")
                if len(snippet) > 200:
                    snippet = snippet[:200] + "…"
                # `SpeechSource.value` is one of the four runtime values, but
                # ``phase_baseline`` is filtered above so the literal narrows
                # to the three viewer-facing values that `RecentSpeech.source`
                # accepts.
                source_value = cast(
                    Literal["text", "voice_stt", "npc_generated"],
                    ev.source.value,
                )
                recent_list.append(
                    RecentSpeech(
                        seat_no=ev.speaker_seat,
                        display_name=name,
                        source=source_value,
                        text=snippet,
                    )
                )
            # Cap to last N so the prompt stays compact even on long phases.
            recent = tuple(recent_list[-_RECENT_SPEECH_CAP:])
        except Exception:
            log.exception("recent_speeches_load_failed phase_id=%s", state.phase_id)

        alive_seats: tuple[tuple[int, str], ...] = ()
        dead_seats: tuple[tuple[int, str], ...] = ()
        role_name: str | None = None
        try:
            seats = await self.repo.load_seats(state.game_id)
            players = await self.repo.load_players(state.game_id)
            seat_name_by_no = {s.seat_no: s.display_name for s in seats}
            alive_list: list[tuple[int, str]] = []
            dead_list: list[tuple[int, str]] = []
            for p in players:
                name = seat_name_by_no.get(p.seat_no, f"席{p.seat_no}")
                if p.alive:
                    alive_list.append((p.seat_no, name))
                else:
                    dead_list.append((p.seat_no, name))
                if p.seat_no == seat_no and p.role is not None:
                    role_name = p.role.value
            alive_seats = tuple(sorted(alive_list))
            dead_seats = tuple(sorted(dead_list))
        except Exception:
            log.exception(
                "seat_role_load_failed game_id=%s seat=%s",
                state.game_id, seat_no,
            )

        role_strategy: str | None = None
        if role_name is not None:
            try:
                role_strategy = build_strategy_block(Role(role_name))
            except Exception:
                log.exception("role_strategy_build_failed role=%s", role_name)
                role_strategy = None

        return recent, alive_seats, dead_seats, role_name, role_strategy

    async def _load_past_votes(
        self, game_id: str, current_day: int
    ) -> tuple[tuple[int, int, tuple[tuple[int, int | None], ...]], ...]:
        """Load completed-day vote ballots so the prompt builder can show
        every NPC the public ledger of "who voted whom".

        Without this, models routinely fabricate their own past vote
        because the EXECUTION public log isn't surfaced anywhere in the
        per-phase fold (state has co_claims and silent_seats but not
        votes). Returns empty when no past day exists or on any DB
        glitch — best-effort.
        """
        if current_day <= 1:
            return ()
        out: list[tuple[int, int, tuple[tuple[int, int | None], ...]]] = []
        try:
            for day in range(1, current_day):
                for round_ in (0, 1):
                    rows = await self.repo.load_votes(
                        game_id, day=day, round_=round_,
                    )
                    if not rows:
                        continue
                    pairs = tuple(
                        (v.voter_seat, v.target_seat)
                        for v in sorted(rows, key=lambda v: v.voter_seat)
                    )
                    out.append((day, round_, pairs))
        except Exception:
            log.exception("past_votes_load_failed game=%s", game_id)
            return ()
        return tuple(out)

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
                    log.exception(
                        "speak_result_response_send_failed npc=%s", result.npc_id)

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
            await self._mark_runoff_done_if_phase(
                game_id=pending.game_id,
                phase_id=result.phase_id,
                seat_no=pending.seat_no,
            )
            return (False, "stale_phase")
        if now > pending.expires_at_ms:
            await _record_rejection("expired_request")
            await self._mark_runoff_done_if_phase(
                game_id=pending.game_id,
                phase_id=result.phase_id,
                seat_no=pending.seat_no,
            )
            return (False, "expired_request")
        if result.status != "accepted" or not result.text:
            await _record_rejection("speaker_declined")
            await self._mark_runoff_done_if_phase(
                game_id=pending.game_id,
                phase_id=result.phase_id,
                seat_no=pending.seat_no,
            )
            return (False, "speaker_declined")
        if len(result.text) > self.config.max_chars_reactive:
            await _record_rejection("utterance_too_long")
            await self._mark_runoff_done_if_phase(
                game_id=pending.game_id,
                phase_id=result.phase_id,
                seat_no=pending.seat_no,
            )
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
        # Resolve the NPC's addressed list: take every seat it named,
        # union with the legacy singular field (for older NPC builds),
        # then drop self-address and any non-alive seats so a
        # hallucinated id can't poison the routing. Order is preserved
        # so the eventual frontmost addressee in the list (e.g.
        # ``[3, 4]``) keeps a deterministic ordering — the arbiter's
        # tiebreak randomises within the addressed group anyway.
        addressed_candidates: list[int] = []
        seen: set[int] = set()
        for seat in result.addressed_seat_nos:
            if seat is None or seat in seen:
                continue
            seen.add(seat)
            addressed_candidates.append(int(seat))
        if (
            result.addressed_seat_no is not None
            and result.addressed_seat_no not in seen
        ):
            seen.add(result.addressed_seat_no)
            addressed_candidates.append(int(result.addressed_seat_no))
        # Drop self-address.
        addressed_candidates = [
            s for s in addressed_candidates if s != pending.seat_no
        ]
        if addressed_candidates:
            try:
                alive_seats = await self.repo.load_players(pending.game_id)
                alive_set = {p.seat_no for p in alive_seats if p.alive}
            except Exception:
                log.exception(
                    "addressed_seat_alive_check_failed game=%s",
                    pending.game_id,
                )
                alive_set = set()
            filtered: list[int] = []
            for s in addressed_candidates:
                if s in alive_set:
                    filtered.append(s)
                else:
                    log.info(
                        "npc_addressed_seat_unknown game=%s seat=%d "
                        "addressed=%s — dropped",
                        pending.game_id, pending.seat_no, s,
                    )
            addressed_candidates = filtered
        addressed_seat_nos: tuple[int, ...] = tuple(addressed_candidates)
        addressed_seat_no = addressed_seat_nos[0] if addressed_seat_nos else None
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
            co_declaration=result.co_declaration,
            addressed_seat_no=addressed_seat_no,
            addressed_seat_nos=addressed_seat_nos,
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
        self._playback_deadlines[result.request_id] = deadline
        authorized = PlaybackAuthorized(
            ts=now,
            trace_id=result.trace_id,
            request_id=result.request_id,
            npc_id=result.npc_id,
            speech_event_id=speech_event.event_id,
            playback_deadline_ms=deadline,
        )
        await _send(authorized.model_dump_json())
        # Runoff candidate: mark this seat's speech done so the engine
        # can advance to DAY_RUNOFF as soon as every tied LLM has spoken.
        # Done after the SpeechEvent is recorded so the runoff_speech log
        # row exists when `plan_runoff_speech_to_runoff` polls progress.
        await self._mark_runoff_done_if_phase(
            game_id=pending.game_id,
            phase_id=result.phase_id,
            seat_no=pending.seat_no,
        )
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
        self._playback_deadlines.pop(msg.request_id, None)
        self._pending.pop(msg.request_id, None)

    async def handle_playback_finished(self, msg: PlaybackFinished) -> None:
        await self.repo.close_npc_playback(
            msg.request_id,
            finished_at_ms=msg.finished_at_ms,
            outcome="succeeded",
            failure_reason=None,
        )
        self._active_playback.discard(msg.request_id)
        self._playback_deadlines.pop(msg.request_id, None)
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
        self._playback_deadlines.pop(msg.request_id, None)
        self._pending.pop(msg.request_id, None)

    # ------------------------------------------------------------- auto-dispatch

    async def _sweep_expired_playback(self) -> None:
        """Close DB rows for playback windows that exceeded their deadline."""
        now = self._now_ms()
        expired = [
            rid for rid, dl in list(self._playback_deadlines.items()) if now > dl
        ]
        for rid in expired:
            log.info("playback_deadline_enforced request=%s", rid)
            try:
                await self.repo.close_npc_playback(
                    rid,
                    finished_at_ms=now,
                    outcome="failed",
                    failure_reason="playback_deadline_exceeded",
                )
            except Exception:
                log.exception("playback_deadline_close_failed request=%s", rid)
            self._active_playback.discard(rid)
            self._playback_deadlines.pop(rid, None)
            self._pending.pop(rid, None)

    async def try_dispatch_next(self, game_id: str) -> None:
        """Auto-pick the next candidate NPC and dispatch a SpeakRequest.

        Called on phase entry, after each new public speech event, and after
        playback completes. No-op when the serial-speech gate is blocked, no
        NPC is online, or no game is in a reactive_voice discussion phase.

        DAY_RUNOFF_SPEECH uses a separate picker (`_dispatch_runoff_next`)
        constrained to tied candidates with one shot each — see that method.
        """
        # Close expired playback rows in DB before checking the gate.
        await self._sweep_expired_playback()

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

        if game.phase is Phase.DAY_RUNOFF_SPEECH:
            await self._dispatch_runoff_next(game_id, game.day_number)
            return

        state = await self.rebuild_public_state(
            game_id=game_id, day=game.day_number, phase=game.phase
        )
        if state is None:
            return

        # Pick the next NPC. Priority order, applied as a 5-key sort:
        #   1. NOT demoted — `_compute_demoted_seats` flags seats stuck
        #      in a low-info pair volley OR exceeding the consecutive
        #      speaker cap. Demoted seats fall to the bottom regardless
        #      of being addressed, so a 3rd NPC can break in.
        #   2. addressed — seat appears in the multi-addressee
        #      ``last_addressed_seats`` set (e.g. 「セツとジナ、どう?」 puts
        #      both 2 and 3 in the set). Both win over non-addressed
        #      seats; randomization on the last axis decides which of the
        #      two goes first.
        #   3. lowest speech_count this phase — generalises the old
        #      binary silent_seats: a 0-count seat is still preferred,
        #      but a 1-count seat now also wins over a 5-count one. Stops
        #      the lowest-seat NPC monopolising once everyone has spoken
        #      once and gives wolf-side seats at higher seat numbers a
        #      fair chance to fake-CO.
        #   4. NOT the immediate previous speaker (LRU rotation).
        #   5. random — replaces the old seat-number tiebreak. Without
        #      randomization the lowest-seat NPC won every tie, so
        #      higher-seat NPCs (e.g. 席8 SQ, 席9 ユリコ) effectively
        #      never spoke. Each call gets a fresh roll so the rotation
        #      is fair across phases.
        addressed_set = state.last_addressed_seats
        last_speaker = state.last_speaker_seat
        demoted = _compute_demoted_seats(state.recent_speech_summary)
        online = self.registry.all_online()

        def _pick_key(e: object) -> tuple[int, int, int, int, float]:
            seat = getattr(e, "assigned_seat", None) or 99
            is_demoted = 1 if seat in demoted else 0
            is_addressed = 0 if seat in addressed_set else 1
            count = state.speech_counts.get(seat, 0)
            is_just_spoke = 1 if (
                last_speaker is not None and seat == last_speaker
            ) else 0
            return (is_demoted, is_addressed, count, is_just_spoke, self._rng.random())

        online_npc_seats = sorted(
            e.assigned_seat
            for e in online
            if e.assigned_seat is not None and e.game_id == game_id
        )
        # Counts per seat, restricted to the candidates the arbiter can
        # actually pick — used both for the snapshot and to classify the
        # reason as ``low_count_rotation`` when the winning seat has
        # spoken but strictly less than someone else online.
        candidate_counts: dict[int, int] = {
            seat: state.speech_counts.get(seat, 0)
            for seat in online_npc_seats
            if seat in state.alive_seat_nos
        }
        max_candidate_count = max(candidate_counts.values(), default=0)
        snapshot: dict[str, Any] = {
            "phase_id": state.phase_id,
            "day": state.day,
            "phase": game.phase.value,
            "last_addressed_seat": (
                next(iter(sorted(addressed_set))) if addressed_set else None
            ),
            "last_addressed_seats": sorted(addressed_set),
            "last_speaker_seat": last_speaker,
            "silent_seats": sorted(state.silent_seats),
            "alive_seat_nos": sorted(state.alive_seat_nos),
            "online_npc_seats": online_npc_seats,
            "demoted_seats": sorted(demoted),
            "speech_counts": sorted(candidate_counts.items()),
        }

        for entry in sorted(online, key=_pick_key):
            if entry.assigned_seat is None or entry.game_id != game_id:
                continue
            if entry.assigned_seat not in state.alive_seat_nos:
                continue
            seat = entry.assigned_seat
            picked_count = state.speech_counts.get(seat, 0)
            if seat in demoted:
                # Reached this branch only when EVERY non-demoted
                # candidate was filtered out (offline / dead / not in
                # this game). Falling back is preferable to silence.
                reason = "all_demoted_fallback"
            elif seat in addressed_set:
                reason = "addressed"
            elif seat in state.silent_seats:
                reason = "silent_rotation"
            elif demoted:
                # The pair-volley gate fired and a non-demoted third
                # party won — labelled distinctly from low_count_rotation
                # so the viewer keeps showing "stuck volley → diverted to
                # seat N" even though the speech_count axis happens to
                # favour the same seat.
                reason = "low_info_diversion"
            elif picked_count < max_candidate_count:
                # Already spoke this phase, but strictly less than some
                # other online candidate — the speech_count axis is what
                # broke the tie. Distinct from silent_rotation (count==0)
                # and from lru_rotation (counts equal, LRU won).
                reason = "low_count_rotation"
            elif last_speaker is not None and seat != last_speaker:
                reason = "lru_rotation"
            else:
                # Seat-number tiebreak was replaced with random in the
                # `_pick_key` so all NPCs at equal priority get a fair
                # roll. Keep the legacy reason label so the viewer's
                # historical badge wording still matches.
                reason = "seat_tiebreak"
            await self.dispatch_request(
                state=state,
                candidate_npc_id=entry.npc_id,
                seat_no=seat,
                game_id=game_id,
                selection_reason=reason,
                public_state_snapshot=snapshot,
            )
            return

    # ------------------------------------------------------------- runoff dispatch

    async def _dispatch_runoff_next(self, game_id: str, day: int) -> None:
        """Dispatch the next tied LLM candidate's final speech.

        Candidate set = round-0 tied seats ∩ alive ∩ LLM seat ∩
        ``runoff_speech_done = False``. The arbiter dispatches them
        sequentially in seat-no order: pick → optional Master narration
        intro → SpeakRequest → NPC TTS playback. After each candidate
        resolves (accepted or any failure path), `runoff_speech_done` is
        flipped so the engine's `plan_runoff_speech_to_runoff` advances
        as soon as the last one finishes.

        When no eligible candidate remains (everyone done, or the only
        ones left have no online NPC bot), `runoff_wake` is fired so the
        engine doesn't sit on the deadline.
        """
        seats = await self.repo.load_seats(game_id)
        seats_by_no: dict[int, Seat] = {s.seat_no: s for s in seats}
        players = await self.repo.load_players(game_id)
        alive_set = {p.seat_no for p in players if p.alive}
        round0 = await self.repo.load_votes(game_id, day=day, round_=0)
        outcome = compute_vote_result(round0, alive_set)
        tied = list(outcome.tied)
        if not tied:
            # Edge: no tied set (e.g. recovery race) → just wake so the
            # engine can re-plan against fresh state.
            self._maybe_wake_runoff(game_id)
            return

        # Tied LLM seats whose runoff speech hasn't been recorded yet.
        # Seat-no order is the user-visible "who speaks first" axis; we
        # follow the same order rounds-mode used so logs / replays line
        # up between modes.
        eligible: list[Seat] = []
        for seat_no in sorted(tied):
            seat = seats_by_no.get(seat_no)
            if seat is None or not seat.is_llm:
                continue
            progress = await self.repo.load_llm_speech_progress(
                game_id, day, seat_no
            )
            if progress[4]:  # runoff_speech_done
                continue
            eligible.append(seat)

        if not eligible:
            # Every tied LLM has already spoken (or been skipped). Wake
            # the engine so DAY_RUNOFF_SPEECH advances to DAY_RUNOFF
            # without waiting for the safety-net deadline.
            self._maybe_wake_runoff(game_id)
            return

        # Find the first eligible seat with an online NPC bot. If a tied
        # candidate has no online NPC (rare: misconfigured persona, bot
        # crash before rejoin), mark them done and skip — otherwise the
        # phase would stall forever.
        chosen: Seat | None = None
        chosen_npc_id: str | None = None
        for seat in eligible:
            entry = self._find_npc_for_seat(game_id, seat.seat_no)
            if entry is None:
                log.info(
                    "runoff_speech_no_online_npc game=%s seat=%d — "
                    "marking done so phase advances",
                    game_id,
                    seat.seat_no,
                )
                try:
                    await self.repo.mark_llm_runoff_speech_done(
                        game_id, day, seat.seat_no
                    )
                except Exception:
                    log.exception(
                        "runoff_speech_done_mark_failed game=%s seat=%d",
                        game_id,
                        seat.seat_no,
                    )
                continue
            chosen = seat
            chosen_npc_id = entry.npc_id
            break

        if chosen is None or chosen_npc_id is None:
            # All remaining tied LLMs were skipped above. Wake the
            # engine so it sees the marks we just wrote.
            self._maybe_wake_runoff(game_id)
            return

        # Master narration: name the candidate before they speak. Awaited
        # so the intro finishes before the NPC's own TTS starts.
        if self._runoff_announce is not None:
            try:
                await self._runoff_announce(chosen)
            except Exception:
                log.exception(
                    "runoff_announce_failed game=%s seat=%d",
                    game_id,
                    chosen.seat_no,
                )

        # Re-rebuild state AFTER the announcement so a fresh
        # phase_baseline / event log is folded in. The state is also
        # what `dispatch_request` consumes for the LogicPacket.
        state = await self.rebuild_public_state(
            game_id=game_id, day=day, phase=Phase.DAY_RUNOFF_SPEECH
        )
        if state is None:
            self._maybe_wake_runoff(game_id)
            return

        snapshot: dict[str, Any] = {
            "phase_id": state.phase_id,
            "day": state.day,
            "phase": Phase.DAY_RUNOFF_SPEECH.value,
            "tied_candidates": sorted(tied),
            "alive_seat_nos": sorted(state.alive_seat_nos),
        }
        request, reason = await self.dispatch_request(
            state=state,
            candidate_npc_id=chosen_npc_id,
            seat_no=chosen.seat_no,
            game_id=game_id,
            selection_reason="runoff_candidate",
            public_state_snapshot=snapshot,
        )
        if request is None:
            # Dispatch failed (npc_offline, ws_send_failed, gate held).
            # The npc_offline + ws_send_failed cases would leave this
            # seat permanently un-spoken, so mark done and re-dispatch
            # so the phase keeps moving. ``queue_busy`` /
            # ``human_currently_speaking`` are transient — just re-poll
            # later via the normal try_dispatch_next pathway.
            if reason in ("npc_offline", "ws_send_failed"):
                await self._mark_runoff_done_if_phase(
                    game_id=game_id,
                    phase_id=state.phase_id,
                    seat_no=chosen.seat_no,
                )
                await self.try_dispatch_next(game_id)
            return
        # Watchdog: if the NPC never returns a SpeakResult before the
        # request's TTL expires, the phase would stall forever. Spawn
        # a one-shot task that marks the seat done and re-dispatches
        # so the engine eventually advances. The mark is idempotent
        # (UPSERT), so a result that arrives in the same window is fine.
        ttl_s = max(1.0, self.config.request_ttl_ms / 1000.0)
        request_id = request.request_id
        seat_no = chosen.seat_no
        phase_id = state.phase_id

        async def _watchdog() -> None:
            try:
                await asyncio.sleep(ttl_s)
            except asyncio.CancelledError:
                return
            if request_id not in self._pending:
                return  # SpeakResult already resolved this slot.
            log.info(
                "runoff_request_watchdog_fired game=%s seat=%d request=%s",
                game_id,
                seat_no,
                request_id,
            )
            await self._mark_runoff_done_if_phase(
                game_id=game_id,
                phase_id=phase_id,
                seat_no=seat_no,
            )
            self._pending.pop(request_id, None)
            try:
                await self.try_dispatch_next(game_id)
            except Exception:
                log.exception(
                    "runoff_watchdog_redispatch_failed game=%s", game_id
                )

        task = asyncio.create_task(_watchdog(), name=f"runoff-watchdog-{request_id}")
        self._runoff_watchdog_tasks.add(task)
        task.add_done_callback(self._runoff_watchdog_tasks.discard)

    def _find_npc_for_seat(
        self, game_id: str, seat_no: int
    ) -> Any | None:
        """Lookup the registry entry pinned to ``(game_id, seat_no)``."""
        for entry in self.registry.all_online():
            if entry.assigned_seat == seat_no and entry.game_id == game_id:
                return entry
        return None

    def _maybe_wake_runoff(self, game_id: str) -> None:
        if self._runoff_wake is None:
            return
        try:
            self._runoff_wake(game_id)
        except Exception:
            log.exception("runoff_wake_failed game=%s", game_id)

    async def _mark_runoff_done_if_phase(
        self,
        *,
        game_id: str,
        phase_id: str,
        seat_no: int,
    ) -> None:
        """Best-effort `runoff_speech_done = 1` for a finished SpeakResult.

        Reads the day from the phase_id format (`gid::dayN::PHASE::seq`)
        so we don't need an extra DB hit. No-op when the phase token in
        ``phase_id`` isn't DAY_RUNOFF_SPEECH (= the result was for an
        ordinary discussion utterance and there's no progress to flip).
        """
        if "::DAY_RUNOFF_SPEECH::" not in phase_id:
            return
        day = _parse_day_from_phase_id(phase_id)
        if day is None:
            return
        try:
            await self.repo.mark_llm_runoff_speech_done(
                game_id, day, seat_no
            )
        except Exception:
            log.exception(
                "runoff_speech_done_mark_failed game=%s day=%d seat=%d",
                game_id,
                day,
                seat_no,
            )
            return
        # Wake the engine so `_plan_next` can re-evaluate immediately
        # whether all tied candidates are done. Without this the phase
        # would sit until the next deadline tick.
        self._maybe_wake_runoff(game_id)

    # ------------------------------------------------------------- game-end cleanup

    def cleanup_game(self, game_id: str) -> int:
        """Drop in-memory speak/playback state belonging to ``game_id``.

        Companion to :meth:`NpcDecisionDispatcher.cleanup_game`. Called
        from the game-end hook so a long-lived Master process doesn't
        carry pending arbitration state across games. The DB rows
        (``npc_speak_requests`` / ``_results`` / ``_playback_events``)
        are intentionally kept for replay/export — only the in-memory
        gates / dicts are swept here.

        Returns the count of in-flight `_pending` entries dropped.
        """
        swept = 0
        for rid, pending in list(self._pending.items()):
            if pending.game_id != game_id:
                continue
            self._pending.pop(rid, None)
            self._active_playback.discard(rid)
            self._playback_deadlines.pop(rid, None)
            swept += 1
        if swept:
            log.info(
                "speak_arbiter_cleanup_game game=%s swept=%d", game_id, swept,
            )
        return swept

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
        before re-entering the arbitration loop. CO claims are layered on
        top from a *game-wide* event scan so day-2+ NPC prompts still show
        the day-1 seer CO etc.; without that carry, the per-phase fold
        starts each new day with empty `co_claims` and wolves miss the
        chance to counter-CO.
        """
        from wolfbot.services.discussion_service import (
            extract_co_claims_from_events,
            rebuild_public_state_from_events,
        )

        phase_id = make_phase_id(game_id, day, phase)
        events: Sequence[SpeechEvent] = await self.discussion.load_phase(game_id, phase_id)
        state = rebuild_public_state_from_events(events)
        if state is None:
            return None
        try:
            all_events = await self.discussion.load_for_game(game_id)
        except Exception:
            log.exception("co_claim_history_load_failed game=%s", game_id)
            all_events = ()
        if all_events:
            state.co_claims = extract_co_claims_from_events(all_events)
        return state


__all__ = ["SpeakArbiter", "SpeakArbiterConfig"]


# Force a non-static reference so the linter keeps Awaitable / Callable
# imports for downstream typing extensions.
_ = (Awaitable, Callable)
