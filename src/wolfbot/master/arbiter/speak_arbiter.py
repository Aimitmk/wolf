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
from wolfbot.master.arbiter.logic_service import build_logic_packet
from wolfbot.master.claim.claim_history import (
    ClaimHistory,
    collect_claim_history,
    expected_medium_claim_count_for_day,
    expected_seer_claim_count_for_day,
)
from wolfbot.master.claim.claim_validator import (
    CO_CAP_REASONS,
    FABRICATION_REASONS,
    ActualMediumEvent,
    ActualSeerEvent,
    validate_claim_against_truth,
)
from wolfbot.master.ws.npc_registry import NpcRegistry
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

# Maximum number of times Master will re-dispatch the same NPC after a
# fabricated `claimed_*_result`. With ~80% pass-rate per attempt
# (empirical: gemini-2.5-flash + thinking_budget=0 on the SEER prompt),
# 5 retries gives P(all fail) ≈ 0.03% — past that we give up and let
# normal rotation pick another NPC so the phase doesn't stall on one
# stuck speaker.
_MAX_FABRICATION_RETRIES = 5


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
    # Was 8000 — too tight for Gemini 2.5 Flash with any thinking
    # budget (game 75a3b1f379cc had p50 latency 14.7s, p90 25s, max
    # 25.7s; 80% of NPC speeches expired at 8s TTL). xAI Grok 4-1-fast
    # comfortably fit under 8s, but the broader model lineup (Gemini
    # 2.5/3, DeepSeek with thinking) routinely needs 15-25s. 30s
    # gives a generous margin while still bounding the phase clock —
    # the discussion phase is 300s so even a worst-case 30s TTL only
    # consumes 10% of the phase per dispatch. Combined with thinking
    # disabled at the env level (NPC_LLM_THINKING_LEVEL=minimal), most
    # responses still come back in 2-4s; the 30s ceiling is purely
    # for tail-latency tolerance.
    request_ttl_ms: int = 30_000
    playback_deadline_ms: int = 12_000
    # Heartbeat freshness gate: an NPC bot is considered offline if its
    # last heartbeat is older than this. Must be ≥3x ``HEARTBEAT_INTERVAL_S``
    # in the NPC env so a single missed/jittered beat doesn't kick the
    # bot offline. Game ``6366cb014a0a`` had ``heartbeat_timeout_ms=5000``
    # against ``HEARTBEAT_INTERVAL_S=5`` (1:1 ratio) — Gina's NPC bot
    # was chronically just-late on heartbeats and got skipped on EVERY
    # SpeakRequest dispatch (0/59 across the whole game) while still
    # receiving DecideVoteRequest (vote dispatch doesn't apply this
    # gate). 15s = 3x interval, the textbook ratio for missed-beat
    # tolerance.
    heartbeat_timeout_ms: int = 15_000
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
        # Per-game tracker of seats already dispatched from the current
        # role-callout priority pool. Reset when `pending_role_callouts`
        # becomes empty for that game (= the callout was consumed by a
        # matching CO). Without this set the picker would loop on the
        # same pool member when others decline; with it, each pool
        # member gets at most one chance per callout.
        self._callout_pool_asked: dict[str, set[int]] = {}
        # Per-(game_id, phase_id, npc_id) count of fabricated-claim
        # rejections. Drives the "same NPC retries until accepted" loop
        # in handle_speak_result. Cleared on phase change (alongside
        # _callout_pool_asked) and on a successful accept for the NPC.
        self._fabrication_retries: dict[tuple[str, str, str], int] = {}
        # Per-(game_id, phase_id) set of seats that hit the fabrication
        # retry cap (`_MAX_FABRICATION_RETRIES`). The dispatcher's
        # candidate picker filters these seats out for the rest of the
        # phase so a chronically-fabricating NPC can't monopolise the
        # phase via the rotation re-selecting them. Cleared on phase
        # change (alongside `_callout_pool_asked`).
        self._fabrication_capped: dict[tuple[str, str], set[int]] = {}
        # Per-(game_id, day) set of seats currently between
        # `_runoff_announce` start and `dispatch_request` (LLM) /
        # human-grace-watchdog spawn (human). The existing `_pending`
        # check inside `_dispatch_runoff_next` only catches re-entries
        # AFTER dispatch_request completes; the entire `await _runoff_announce`
        # window (Levi TTS, several seconds) is unguarded. Without this
        # set, a second `try_dispatch_next` invocation triggered by
        # `_master_narrate` (PHASE_CHANGE) racing against
        # `_on_reactive_phase_enter` re-picks the same seat and
        # awaits `_runoff_announce` again, causing Levi to read the
        # candidate intro twice in a row before the NPC speaks.
        self._runoff_in_flight: dict[tuple[str, int], set[int]] = {}
        # Per-(game_id, day) set of human candidates whose grace
        # watchdog is currently scheduled. Strong refs to the asyncio
        # tasks live in `_runoff_watchdog_tasks`; this dict just dedups.
        self._runoff_human_watchdog: dict[tuple[str, int], set[int]] = {}

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
        expired_stt = [sid for sid, dl in self._pending_stt_segments.items() if now > dl]
        for sid in expired_stt:
            log.info("stt_finalization_timeout segment=%s", sid)
            self._pending_stt_segments.pop(sid, None)
            self._human_speaking_segments.discard(sid)
        if self._human_speaking_segments:
            return "human_currently_speaking"
        # Sweep expired playback deadlines — close rows whose NPC never
        # reported tts_failed / playback_finished / playback_failed.
        expired_pb = [rid for rid, dl in self._playback_deadlines.items() if now > dl]
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
        retry_feedback: str | None = None,
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
        (
            recent_speeches,
            alive_seats,
            dead_seats,
            role_name,
            role_strategy,
        ) = await self._collect_request_context(state, seat_no)
        past_votes = await self._load_past_votes(game_id, state.day)
        past_suspicions = await self._load_past_suspicions(game_id)
        seat_names_lookup: dict[int, str] = {
            seat: name for seat, name in (list(alive_seats) + list(dead_seats))
        }
        claim_history = await self._load_claim_history(game_id, seat_names_lookup)

        # Build LogicPacket (sent first so the NPC has context for the
        # subsequent speak_request).
        packet = build_logic_packet(
            state=state,
            recipient_npc_id=candidate_npc_id,
            expires_at_ms=now + self.config.request_ttl_ms,
            now_ms=now,
            recent_speeches=recent_speeches,
            past_votes=past_votes,
            past_suspicions=past_suspicions,
            seat_names=seat_names_lookup,
            claim_history=claim_history,
            recipient_seat_no=seat_no,
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
            retry_feedback=retry_feedback,
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
                state.game_id,
                seat_no,
            )

        role_strategy: str | None = None
        if role_name is not None:
            try:
                role_strategy = build_strategy_block(Role(role_name))
            except Exception:
                log.exception("role_strategy_build_failed role=%s", role_name)
                role_strategy = None

        return recent, alive_seats, dead_seats, role_name, role_strategy

    async def _count_execution_days(self, game_id: str) -> int:
        """Count distinct days that recorded an EXECUTION public log.

        Used by the pending-result picker priority: a medium CO seat is
        "pending" when their published medium-claim count is short of the
        executions-so-far. We cap the lookup at the limit hard-coded in
        ``load_public_logs`` (40 rows is plenty — a 9-player game caps
        out at 4 ropes ≤ 4 executions plus runoffs). On error we return
        0 so the priority bucket stays empty and the picker falls back
        to the count-driven rotation.
        """
        try:
            rows = await self.repo.load_public_logs(game_id, limit=40)
        except Exception:
            log.exception("execution_day_count_failed game=%s", game_id)
            return 0
        days = {
            int(r["day"])
            for r in rows
            if r.get("kind") == "EXECUTION" and r.get("day") is not None
        }
        return len(days)

    def _compute_pending_result_seats(
        self,
        *,
        co_claims: Sequence[Any],
        claim_history: ClaimHistory | None,
        day_number: int,
        execution_days: int,
    ) -> frozenset[int]:
        """Seats with an unpublished seer/medium result for the current day.

        A seer CO seat is pending when their announced seer-claim count
        is strictly less than ``expected_seer_claim_count_for_day`` —
        i.e. they haven't yet declared today's expected divine result
        (NIGHT_0 random white on day 1, prior-night result on day 2+).

        A medium CO seat is pending when their announced medium-claim
        count is strictly less than ``executions_so_far`` — i.e. there
        was an execution they haven't yet weighed in on. day 1 has no
        prior execution so the medium can never be pending in day 1.

        Returns an empty set when ``claim_history`` is None (load
        failure) or no CO matches; the picker treats an empty set as
        "no priority bucket" and proceeds with the count-driven sort.
        """
        if claim_history is None or day_number < 1:
            return frozenset()
        expected_seer = expected_seer_claim_count_for_day(day_number)
        expected_medium = expected_medium_claim_count_for_day(execution_days)
        pending: set[int] = set()
        for claim in co_claims:
            seat = getattr(claim, "seat", None)
            role = getattr(claim, "role_claim", None)
            if seat is None or role is None:
                continue
            history = claim_history.by_seat.get(seat)
            if history is None:
                # CO'd but no claim entries persisted yet — definitely pending.
                if role in ("seer", "medium"):
                    pending.add(seat)
                continue
            if (role == "seer" and len(history.seer_claims) < expected_seer) or (role == "medium" and len(history.medium_claims) < expected_medium):
                pending.add(seat)
        return frozenset(pending)

    async def _load_claim_history(
        self,
        game_id: str,
        seat_names: dict[int, str],
    ) -> ClaimHistory | None:
        """Aggregate every persisted seer/medium claim into a per-seat history.

        Reads ``speech_events`` directly via the discussion-service store
        so the rebuild matches the canonical SpeechEvent ordering. The
        helper is best-effort: a load failure logs and returns ``None``
        so the per-prompt block silently degrades to the legacy "no
        history" rendering rather than failing the whole dispatch.
        """
        try:
            events = await self.discussion.load_for_game(game_id)
        except Exception:
            log.exception(
                "claim_history_load_failed game=%s",
                game_id,
            )
            return None
        return collect_claim_history(events, seat_names=seat_names)

    async def _load_past_suspicions(
        self, game_id: str
    ) -> tuple[tuple[int, str, int, int, str, str, str | None, str | None], ...]:
        """Load the public suspicion timeline for the game.

        Best-effort: returns an empty tuple on any DB glitch so a
        suspicions-table read failure cannot stall a SpeakRequest.
        """
        try:
            rows = await self.repo.load_suspicions_for_game(game_id)
        except Exception:
            log.exception("past_suspicions_load_failed game=%s", game_id)
            return ()
        return tuple(
            (
                int(r["day"]),
                str(r["phase"]),
                int(r["suspecter_seat"]),
                int(r["target_seat"]),
                str(r["level"]),
                str(r["reason"]),
                r["update_from_level"] if r["update_from_level"] is not None else None,
                r["update_reason"] if r["update_reason"] is not None else None,
            )
            for r in rows
        )

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
                        game_id,
                        day=day,
                        round_=round_,
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

    async def _load_actual_seer_history(
        self,
        game_id: str,
        seat_no: int,
    ) -> list[ActualSeerEvent]:
        """Build the real-seer history for `seat_no`.

        Reuses :func:`master.private_state.load_private_state_for_seat`
        so the validator reads the SAME private-log path the NPC's
        ``自分の占い結果`` prompt section sees: ``SEER_RESULT_NIGHT0``
        plus per-night ``SEER_RESULT`` rows in ``logs_private``.

        Going through ``night_actions`` directly (the v1 implementation)
        misses the NIGHT_0 random white because no submission row is
        written for it — the system picks the target. Game
        ``6a0dd72d63e3`` reproduced the bug: real seer Setsu's NIGHT_0
        Jonas白 wasn't in ``night_actions``, so the validator treated
        her legitimate Jonas claim as a fabricated target and bounced
        her 6 times in a row.
        """
        from wolfbot.master.state.private_state import load_private_state_for_seat
        try:
            players = await self.repo.load_players(game_id)
            seats = await self.repo.load_seats(game_id)
        except Exception:
            log.exception("actual_seer_load_meta_failed game=%s", game_id)
            return []
        try:
            seer_results, _medium, _guard, _wolfchat, _attacks = (
                await load_private_state_for_seat(
                    self.repo, game_id=game_id, seat_no=seat_no,
                    role=Role.SEER, players=players, seats=seats,
                )
            )
        except Exception:
            log.exception(
                "actual_seer_load_private_state_failed game=%s seat=%s",
                game_id, seat_no,
            )
            return []
        return [
            ActualSeerEvent(
                day=sr.day,
                target_seat=sr.target_seat,
                is_wolf=sr.is_wolf,
            )
            for sr in seer_results
        ]

    async def _load_actual_medium_history(
        self,
        game_id: str,
        seat_no: int,
    ) -> tuple[list[ActualMediumEvent], int]:
        """Build real-medium history + total executions-so-far count.

        Mirrors :meth:`_load_actual_seer_history` for medium: parse
        ``MEDIUM_RESULT`` rows in ``logs_private`` so the validator
        and the NPC's ``自分の霊媒結果`` prompt section agree on truth.
        For the ``executions_so_far`` count needed by fake-medium
        guards, we additionally count public EXECUTION logs because
        non-medium speakers don't have private medium logs. Returns
        ``(history, count)`` — history empty unless the speaker IS
        the real medium, count comes from the public log regardless.
        """
        from wolfbot.master.state.private_state import load_private_state_for_seat
        try:
            players = await self.repo.load_players(game_id)
            seats = await self.repo.load_seats(game_id)
        except Exception:
            log.exception("actual_medium_load_meta_failed game=%s", game_id)
            return ([], 0)
        try:
            _seer, medium_results, _guard, _wolfchat, _attacks = (
                await load_private_state_for_seat(
                    self.repo, game_id=game_id, seat_no=seat_no,
                    role=Role.MEDIUM, players=players, seats=seats,
                )
            )
        except Exception:
            log.exception(
                "actual_medium_load_private_state_failed game=%s seat=%s",
                game_id, seat_no,
            )
            medium_results = ()
        # Real-medium results may include "no execution today" entries
        # whose is_wolf is None — those don't need validating against
        # because they carry no claim-able fact, so drop them here.
        history = [
            ActualMediumEvent(
                day=mr.day,
                target_seat=mr.target_seat,
                is_wolf=mr.is_wolf,
            )
            for mr in medium_results
            if mr.is_wolf is not None
        ]
        # Public EXECUTION count is needed even when the speaker isn't
        # the real medium (fake-medium "no execution yet" rule).
        count = 0
        try:
            logs = await self.repo.load_public_logs(game_id, limit=200)
        except Exception:
            log.exception("actual_medium_load_logs_failed game=%s", game_id)
            logs = []
        for row in logs:
            if row.get("kind") == "EXECUTION":
                count += 1
        return (history, count)

    async def _load_speaker_role(self, game_id: str, seat_no: int) -> Role | None:
        try:
            players = await self.repo.load_players(game_id)
        except Exception:
            log.exception("speaker_role_load_failed game=%s", game_id)
            return None
        for p in players:
            if p.seat_no == seat_no:
                return p.role
        return None

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

        async def _reject_and_advance(reason: str) -> tuple[bool, str | None]:
            """Common rejection cleanup: record + mark runoff (no-op outside
            runoff) + pop _pending + re-dispatch.

            Without the pop + re-dispatch, an `expired_request` rejection
            in DAY_DISCUSSION leaves the phase silently stalled — game
            eab1f9514a10 day 4: SQ finished, ユリコ was dispatched, took
            10s to respond (TTL=8s), got `expired_request` rejected, and
            the day went 2.5 minutes without a single follow-up dispatch
            because no one was triggering try_dispatch_next.
            """
            await _record_rejection(reason)
            await self._mark_runoff_done_if_phase(
                game_id=pending.game_id,
                phase_id=result.phase_id,
                seat_no=pending.seat_no,
            )
            self._pending.pop(result.request_id, None)
            try:
                await self.try_dispatch_next(pending.game_id)
            except Exception:
                log.exception(
                    "speak_result_rejection_redispatch_failed game=%s reason=%s",
                    pending.game_id,
                    reason,
                )
            return (False, reason)

        async def _reject_and_retry_same_npc(
            reason: str,
            feedback: str,
        ) -> tuple[bool, str | None]:
            """Same-NPC retry path for fabrication-class rejections.

            Records the rejection (so the audit trail keeps every
            attempt), sends a ``PlaybackRejected`` so the NPC drops
            its queued audio, then re-dispatches a fresh SpeakRequest
            to the same seat with ``retry_feedback`` filled in. The
            rebuilt LogicPacket carries the latest public state (the
            previous attempt's text is NOT in the public log because
            it never played, so the recent-speech section is unchanged
            from the original attempt). Increments
            ``_fabrication_retries`` first; the caller checked the cap
            already, so this method is the unconditional retry leg.
            """
            await _record_rejection(reason)
            self._pending.pop(result.request_id, None)
            try:
                state_for_retry = await self.rebuild_public_state(
                    game_id=pending.game_id,
                    day=day,
                    phase=phase,
                )
                if state_for_retry is None:
                    log.info(
                        "fabrication_retry_no_state game=%s npc=%s",
                        pending.game_id,
                        result.npc_id,
                    )
                    return (False, reason)
                await self.dispatch_request(
                    state=state_for_retry,
                    candidate_npc_id=result.npc_id,
                    seat_no=pending.seat_no,
                    game_id=pending.game_id,
                    suggested_intent="speak",
                    selection_reason="fabrication_retry",
                    retry_feedback=feedback,
                )
            except Exception:
                log.exception(
                    "fabrication_retry_dispatch_failed game=%s npc=%s reason=%s",
                    pending.game_id,
                    result.npc_id,
                    reason,
                )
            return (False, reason)

        if result.phase_id != current_phase_id:
            return await _reject_and_advance("stale_phase")
        if now > pending.expires_at_ms:
            return await _reject_and_advance("expired_request")
        if result.status != "accepted" or not result.text:
            return await _reject_and_advance("speaker_declined")
        if len(result.text) > self.config.max_chars_reactive:
            return await _reject_and_advance("utterance_too_long")

        # Fabrication validation: catch a real seer / medium claiming an
        # unrecorded target, OR a fake CO swapping its own past target /
        # color. On hit, retry the same NPC up to _MAX_FABRICATION_RETRIES
        # times with feedback in the next prompt; past the cap, fall
        # back to normal rotation so the phase doesn't stall.
        if result.claimed_seer_result is not None or result.claimed_medium_result is not None:
            speaker_role = await self._load_speaker_role(
                pending.game_id,
                pending.seat_no,
            )
            actual_seer: list[ActualSeerEvent] = []
            actual_medium: list[ActualMediumEvent] = []
            executions_so_far = 0
            if speaker_role is Role.SEER:
                actual_seer = await self._load_actual_seer_history(
                    pending.game_id, pending.seat_no,
                )
            if speaker_role is Role.MEDIUM:
                actual_medium, executions_so_far = await self._load_actual_medium_history(
                    pending.game_id, pending.seat_no,
                )
            else:
                # Need executions_so_far for the fake-medium "no execution
                # yet" rule even when the speaker isn't the real medium.
                _, executions_so_far = await self._load_actual_medium_history(
                    pending.game_id, pending.seat_no,
                )
            seats = await self.repo.load_seats(pending.game_id)
            seat_names_for_history = {s.seat_no: s.display_name for s in seats}
            claim_history = await self._load_claim_history(
                pending.game_id,
                seat_names_for_history,
            )
            prior_for_speaker = (
                claim_history.by_seat.get(pending.seat_no) if claim_history is not None else None
            )
            # Count distinct claimers per role for the global CO cap
            # (max 3 seer / max 2 medium). A seat with non-empty
            # seer_claims has CO'd at least once → counts as one
            # distinct claimer.
            seer_co_count = 0
            medium_co_count = 0
            if claim_history is not None:
                for ch in claim_history.by_seat.values():
                    if ch.seer_claims:
                        seer_co_count += 1
                    if ch.medium_claims:
                        medium_co_count += 1
            validation = validate_claim_against_truth(
                speaker_role=speaker_role or Role.VILLAGER,
                speaker_seat=pending.seat_no,
                day=day,
                phase=phase,
                claimed_seer=result.claimed_seer_result,
                claimed_medium=result.claimed_medium_result,
                actual_seer_history=actual_seer,
                actual_medium_history=actual_medium,
                prior_public_claims=prior_for_speaker,
                executions_so_far=executions_so_far,
                seer_co_count=seer_co_count,
                medium_co_count=medium_co_count,
            )
            if not validation.ok and validation.reason in CO_CAP_REASONS:
                # CO cap exceeded: drop the audio (PlaybackRejected) and
                # rotate to the next picker pick. Don't burn fabrication
                # retries — the cap is a structural rule, not a self-
                # correction problem. The same NPC remains eligible for
                # subsequent dispatches; if they CO again next time,
                # they get rejected again, but no retry cascade happens.
                log.info(
                    "co_cap_exceeded_skip game=%s npc=%s seat=%s reason=%s",
                    pending.game_id,
                    result.npc_id,
                    pending.seat_no,
                    validation.reason,
                )
                return await _reject_and_advance(validation.reason)
            if not validation.ok and validation.reason in FABRICATION_REASONS:
                key = (pending.game_id, result.phase_id, result.npc_id)
                self._fabrication_retries[key] = self._fabrication_retries.get(key, 0) + 1
                attempt = self._fabrication_retries[key]
                log.warning(
                    "fabrication_detected game=%s npc=%s seat=%s reason=%s attempt=%d/%d",
                    pending.game_id,
                    result.npc_id,
                    pending.seat_no,
                    validation.reason,
                    attempt,
                    _MAX_FABRICATION_RETRIES,
                )
                if attempt >= _MAX_FABRICATION_RETRIES:
                    # Give up: block this seat from being re-picked for
                    # the rest of the phase, then bail to normal rotation.
                    # Without the block, `try_dispatch_next` re-selects
                    # the same NPC immediately (especially when they're
                    # in the seer/medium callout pool), the retry counter
                    # increments past the cap (observed: attempt=6/5,
                    # 7/5, 8/5...), and the phase stalls because no other
                    # NPC ever gets dispatched. Game ``6366cb014a0a``
                    # day 1 hit this with ユリコ (madman) burning ~75s
                    # of phase time on `day1_seer_claim_overflow` retries
                    # before the deadline closed the discussion.
                    self._fabrication_capped.setdefault(
                        (pending.game_id, result.phase_id), set()
                    ).add(pending.seat_no)
                    log.info(
                        "fabrication_capped_seat_blocked game=%s phase=%s seat=%s",
                        pending.game_id, result.phase_id, pending.seat_no,
                    )
                    return await _reject_and_advance(validation.reason)
                return await _reject_and_retry_same_npc(
                    reason=validation.reason,
                    feedback=validation.feedback or "",
                )

        # Accepted. Clear any prior fabrication-retry count for this
        # (game, phase, npc) so the cap resets if the NPC fabricates
        # again later in the same phase.
        self._fabrication_retries.pop(
            (pending.game_id, result.phase_id, result.npc_id),
            None,
        )

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
        if result.addressed_seat_no is not None and result.addressed_seat_no not in seen:
            seen.add(result.addressed_seat_no)
            addressed_candidates.append(int(result.addressed_seat_no))
        # Drop self-address.
        addressed_candidates = [s for s in addressed_candidates if s != pending.seat_no]
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
                        "npc_addressed_seat_unknown game=%s seat=%d addressed=%s — dropped",
                        pending.game_id,
                        pending.seat_no,
                        s,
                    )
            addressed_candidates = filtered
        addressed_seat_nos: tuple[int, ...] = tuple(addressed_candidates)
        addressed_seat_no = addressed_seat_nos[0] if addressed_seat_nos else None
        # Self-claim guard: if the NPC's structured claim names its own
        # seat we drop it before persisting. The wire model already
        # rejects self-target via `_build_claimed_*` on the NPC side, but
        # an older NPC binary or a malformed payload could still slip
        # through — defending here keeps the claim-history fold clean.
        seer_claim = result.claimed_seer_result
        if seer_claim is not None and seer_claim.target_seat == pending.seat_no:
            seer_claim = None
        medium_claim = result.claimed_medium_result
        if medium_claim is not None and medium_claim.target_seat == pending.seat_no:
            medium_claim = None
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
            claimed_seer_target_seat=(seer_claim.target_seat if seer_claim is not None else None),
            claimed_seer_is_wolf=(seer_claim.is_wolf if seer_claim is not None else None),
            claimed_medium_target_seat=(
                medium_claim.target_seat if medium_claim is not None else None
            ),
            claimed_medium_is_wolf=(medium_claim.is_wolf if medium_claim is not None else None),
            created_at_ms=now,
        )
        await self.discussion.record(speech_event)
        # Persist structured suspicions attached to this utterance. Drop
        # entries that target the speaker's own seat (the SpeakResult
        # builder already filters but a stale NPC binary could slip
        # through). Empty list is a no-op.
        valid_suspicions = tuple(
            s for s in result.suspicions if s.target_seat != pending.seat_no
        )
        if valid_suspicions:
            try:
                await self.repo.insert_speech_suspicions(
                    event_id=speech_event.event_id,
                    game_id=pending.game_id,
                    day=day,
                    phase=phase,
                    suspecter_seat=pending.seat_no,
                    created_at_ms=now,
                    suspicions=valid_suspicions,
                )
            except Exception:
                log.exception(
                    "speech_suspicions_insert_failed game=%s seat=%d event=%s",
                    pending.game_id,
                    pending.seat_no,
                    speech_event.event_id,
                )
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
        # Note: `runoff_speech_done` is intentionally NOT flipped here.
        # `accept_speak_result` fires when the NPC has generated text +
        # TTS bytes — the audio hasn't started playing in VC yet. If we
        # advanced the phase here, the runoff vote would open while the
        # candidate's speech is still being read out loud. Defer the
        # flag flip to `handle_playback_finished` (or
        # `handle_playback_failed` / `handle_tts_failed`) so listeners
        # actually hear the final speech before the vote starts.
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
        # Read pending fields BEFORE pop so the runoff-mark hook
        # below has the seat / phase / game ids available. TTS-fail
        # is a terminal state; the phase advances rather than stalling.
        pending = self._pending.get(msg.request_id)
        self._active_playback.discard(msg.request_id)
        self._playback_deadlines.pop(msg.request_id, None)
        self._pending.pop(msg.request_id, None)
        if pending is not None:
            await self._mark_runoff_done_if_phase(
                game_id=pending.game_id,
                phase_id=pending.phase_id,
                seat_no=pending.seat_no,
            )

    async def handle_playback_finished(self, msg: PlaybackFinished) -> None:
        await self.repo.close_npc_playback(
            msg.request_id,
            finished_at_ms=msg.finished_at_ms,
            outcome="succeeded",
            failure_reason=None,
        )
        # Read pending fields BEFORE pop so the runoff-mark hook
        # has the seat / phase / game ids. The flag flip is delayed
        # to playback-finish (not accept_speak_result) so listeners
        # actually hear the final speech before the vote starts.
        pending = self._pending.get(msg.request_id)
        self._active_playback.discard(msg.request_id)
        self._playback_deadlines.pop(msg.request_id, None)
        self._pending.pop(msg.request_id, None)
        if pending is not None:
            await self._mark_runoff_done_if_phase(
                game_id=pending.game_id,
                phase_id=pending.phase_id,
                seat_no=pending.seat_no,
            )

    async def handle_playback_failed(self, msg: PlaybackFailed) -> None:
        now = self._now_ms()
        await self.repo.close_npc_playback(
            msg.request_id,
            finished_at_ms=now,
            outcome="failed",
            failure_reason=msg.failure_reason,
        )
        pending = self._pending.get(msg.request_id)
        self._active_playback.discard(msg.request_id)
        self._playback_deadlines.pop(msg.request_id, None)
        self._pending.pop(msg.request_id, None)
        if pending is not None:
            # Playback fail is terminal — advance instead of stalling.
            await self._mark_runoff_done_if_phase(
                game_id=pending.game_id,
                phase_id=pending.phase_id,
                seat_no=pending.seat_no,
            )

    # ------------------------------------------------------------- auto-dispatch

    async def _sweep_expired_playback(self) -> None:
        """Close DB rows for playback windows that exceeded their deadline."""
        now = self._now_ms()
        expired = [rid for rid, dl in list(self._playback_deadlines.items()) if now > dl]
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

        # Pick the next NPC. Priority order (applied as a 7-key sort):
        #   1. callout pool member — seer/medium/knight callout in flight
        #      and this seat is in the priority pool (real role-holder +
        #      wolf-side fake-CO candidates).
        #   2. NOT demoted — `_compute_demoted_seats` flags seats stuck
        #      in a low-info pair volley OR exceeding the consecutive
        #      speaker cap. Demoted seats fall to the bottom so a 3rd
        #      NPC can break in.
        #   3. **pending role result** — a seer/medium CO seat that
        #      hasn't yet published today's expected ability result.
        #      Promoted above addressed/count so a CO holder always gets
        #      a turn before anyone else when they owe a published
        #      result (real seer day-2 silence is the case this fixes —
        #      see game c99ecf313f96 day 2 ラキオ占いCO never spoke).
        #   4. lowest speech_count this phase — primary rotation key
        #      across all days (not just day 1). Was previously below
        #      ``addressed`` which let role-CO seats addressing each
        #      other monopolise the floor while non-CO greys stayed at
        #      0 turns; promoting count to before addressed guarantees
        #      everyone speaks before anyone speaks twice (same-day).
        #   5. addressed — seat appears in ``last_addressed_seats``.
        #      Now a tiebreaker: an addressed seat with count=N wins
        #      over a non-addressed seat with the same count, but a
        #      non-addressed seat with count<N wins regardless of
        #      addressing. Weakens the previous "addressed dominates"
        #      behaviour that caused CO-vs-CO ping-pong.
        #   6. NOT the immediate previous speaker (LRU rotation).
        #   7. random — fair tiebreak across personas.
        addressed_set = state.last_addressed_seats
        last_speaker = state.last_speaker_seat
        demoted = _compute_demoted_seats(state.recent_speech_summary)
        online = self.registry.all_online()
        # Load the data needed to compute the pending-result priority
        # bucket. Failures degrade gracefully: claim_history=None /
        # execution_days=0 → empty pending_set → picker falls through
        # to the count-driven sort exactly as before.
        seat_names_for_claim = {
            entry.assigned_seat: "" for entry in online if entry.assigned_seat is not None
        }
        claim_history = await self._load_claim_history(game_id, seat_names_for_claim)
        execution_days = await self._count_execution_days(game_id)
        pending_results = self._compute_pending_result_seats(
            co_claims=state.co_claims,
            claim_history=claim_history,
            day_number=state.day,
            execution_days=execution_days,
        )

        # Role-callout priority pool: when someone publicly asks for a
        # seer/medium CO and that role hasn't been claimed yet, prioritize
        # the real role-holder + every wolf-side seat that hasn't CO'd as
        # any info role. Picks are random within the pool so the village
        # can't meta-read "first NPC to speak after a callout = real".
        # Each pool member gets at most one chance per callout (tracked
        # via `_callout_pool_asked`); if everyone declines the pool
        # exhausts and normal rotation resumes.
        callout_pool = await self._compute_callout_pool(game_id, state)
        asked = self._callout_pool_asked.get(game_id, set())
        # Reset the asked tracker when there's no active priority signal
        # AND no remaining unasked pool members. Without the second leg
        # of the conjunction, the reset would fire mid-pool whenever
        # ``pending_role_callouts`` cleared (= a matching CO arrived) and
        # erase the asked-record while ``pending_co_response`` was still
        # rotating wolf-side seats — the just-asked seat would get
        # re-picked on the very next dispatch.
        unasked_remaining = bool(callout_pool - asked)
        no_active_signal = not state.pending_role_callouts and not state.pending_co_response
        if no_active_signal and asked and not unasked_remaining:
            self._callout_pool_asked.pop(game_id, None)
            asked = set()
        effective_pool = callout_pool - asked

        def _pick_key(e: object) -> tuple[int, int, int, int, int, int, float]:
            seat = getattr(e, "assigned_seat", None) or 99
            is_in_pool = 0 if seat in effective_pool else 1
            is_demoted = 1 if seat in demoted else 0
            is_pending = 0 if seat in pending_results else 1
            count = state.speech_counts.get(seat, 0)
            is_addressed = 0 if seat in addressed_set else 1
            is_just_spoke = 1 if (last_speaker is not None and seat == last_speaker) else 0
            return (
                is_in_pool,
                is_demoted,
                is_pending,
                count,
                is_addressed,
                is_just_spoke,
                self._rng.random(),
            )

        online_npc_seats = sorted(
            e.assigned_seat for e in online if e.assigned_seat is not None and e.game_id == game_id
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
            "last_addressed_seat": (next(iter(sorted(addressed_set))) if addressed_set else None),
            "last_addressed_seats": sorted(addressed_set),
            "last_speaker_seat": last_speaker,
            "silent_seats": sorted(state.silent_seats),
            "alive_seat_nos": sorted(state.alive_seat_nos),
            "online_npc_seats": online_npc_seats,
            "demoted_seats": sorted(demoted),
            "pending_result_seats": sorted(pending_results),
            "speech_counts": sorted(candidate_counts.items()),
        }

        capped_for_phase: frozenset[int] | set[int] = self._fabrication_capped.get(
            (game_id, state.phase_id), frozenset()
        )
        for entry in sorted(online, key=_pick_key):
            if entry.assigned_seat is None or entry.game_id != game_id:
                continue
            if entry.assigned_seat not in state.alive_seat_nos:
                continue
            if entry.assigned_seat in capped_for_phase:
                # Hit the fabrication cap this phase — skip so a chronically
                # fabricating NPC can't monopolise rotation. Cleared when the
                # phase advances (see cleanup_game / phase-change reset).
                continue
            seat = entry.assigned_seat
            picked_count = state.speech_counts.get(seat, 0)
            if seat in effective_pool:
                # Role-callout pool member won — record so future picks
                # in this same callout window pick a different member.
                self._callout_pool_asked.setdefault(game_id, set()).add(seat)
                reason = "role_callout_pool"
            elif seat in demoted:
                # Reached this branch only when EVERY non-demoted
                # candidate was filtered out (offline / dead / not in
                # this game). Falling back is preferable to silence.
                reason = "all_demoted_fallback"
            elif seat in pending_results:
                # Seer/medium CO seat owing today's published ability
                # result — promoted above count + addressed so the floor
                # is held until the result lands.
                reason = "pending_role_result"
            elif demoted:
                # The pair-volley gate fired and a non-demoted third
                # party won. Labelled before the count-based labels so
                # the viewer keeps showing "stuck volley → diverted to
                # seat N" even when the count axis also discriminates
                # (the demotion is the *cause* of the diversion; count
                # would only be the rationale absent the volley).
                reason = "low_info_diversion"
            elif picked_count < max_candidate_count:
                # Lower count than the top of the field — this is now the
                # primary rotation lever. silent_rotation (count==0) is
                # a special case of low_count_rotation worth labelling
                # so the viewer can still show "first speech of the
                # phase".
                reason = "silent_rotation" if picked_count == 0 else "low_count_rotation"
            elif seat in addressed_set:
                # Counts tied at the top — addressing wins the tiebreak.
                reason = "addressed"
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
        """Dispatch the next tied candidate's final speech (LLM or human).

        Candidate set = round-0 tied seats ∩ alive ∩
        ``runoff_speech_done = False``. The arbiter walks them in
        seat-no order: pick → Master narration intro → (LLM:
        SpeakRequest → NPC TTS) / (human: grace-watchdog awaits a
        SpeechEvent or marks done on timeout). After each candidate
        resolves, `runoff_speech_done` is flipped so the engine's
        `plan_runoff_speech_to_runoff` advances as soon as the last
        one finishes.

        Human candidates are first-class participants. They get a Levi
        intro and a grace window (`runoff_speech_grace`) to actually
        speak via voice / text; if they speak the SpeechEvent path
        flips the flag, otherwise the watchdog marks them done so the
        phase doesn't stall.
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

        # Auto-mark already-spoken human candidates. Voice (ingest_service)
        # and text (discord_service.on_message) both record SpeechEvents
        # via `discussion.record()` but neither flips
        # `runoff_speech_done` on the human path (that flag is the
        # NPC-side dispatcher's accounting). Detect their presence here
        # by scanning this phase's speech_events — once a tied human
        # has any non-baseline event from their seat in this phase,
        # consider their turn complete and flip the flag so the engine
        # can advance.
        phase_id = make_phase_id(game_id, day, Phase.DAY_RUNOFF_SPEECH)
        try:
            phase_events = await self.discussion.load_phase(game_id, phase_id)
        except Exception:
            phase_events = []
        human_spoke_seats: set[int] = set()
        for ev in phase_events:
            if ev.speaker_seat is None:
                continue
            seat = seats_by_no.get(ev.speaker_seat)
            if seat is None or seat.is_llm:
                continue
            human_spoke_seats.add(ev.speaker_seat)
        for seat_no in sorted(human_spoke_seats & set(tied)):
            try:
                await self.repo.mark_llm_runoff_speech_done(game_id, day, seat_no)
            except Exception:
                log.exception(
                    "runoff_human_mark_done_failed game=%s seat=%d",
                    game_id,
                    seat_no,
                )

        # Tied seats whose runoff speech hasn't been recorded yet.
        # Seat-no order is the user-visible "who speaks first" axis.
        eligible: list[Seat] = []
        for seat_no in sorted(tied):
            seat = seats_by_no.get(seat_no)
            if seat is None:
                continue
            progress = await self.repo.load_llm_speech_progress(game_id, day, seat_no)
            if progress[4]:  # runoff_speech_done
                continue
            eligible.append(seat)

        if not eligible:
            # Every tied candidate has already spoken (or been skipped).
            # Wake the engine so DAY_RUNOFF_SPEECH advances to DAY_RUNOFF
            # without waiting for the safety-net deadline.
            self._maybe_wake_runoff(game_id)
            return

        # Find the first eligible seat that's actually dispatchable.
        # LLM seats need an online NPC bot (mark done + skip if absent —
        # otherwise the phase would stall forever). Human seats are
        # always dispatchable: their grace watchdog handles silence.
        chosen: Seat | None = None
        chosen_npc_id: str | None = None
        for seat in eligible:
            if not seat.is_llm:
                chosen = seat
                break
            entry = self._find_npc_for_seat(game_id, seat.seat_no)
            if entry is None:
                log.info(
                    "runoff_speech_no_online_npc game=%s seat=%d — marking done so phase advances",
                    game_id,
                    seat.seat_no,
                )
                try:
                    await self.repo.mark_llm_runoff_speech_done(game_id, day, seat.seat_no)
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

        if chosen is None:
            # All remaining tied LLMs were skipped above. Wake the
            # engine so it sees the marks we just wrote.
            self._maybe_wake_runoff(game_id)
            return

        # Re-entrancy guard #1 (post-dispatch): a SpeakRequest for this
        # seat is already in `_pending` — the existing dispatch will
        # flip `runoff_speech_done` itself. LLM-only.
        for pending in self._pending.values():
            if pending.game_id == game_id and pending.seat_no == chosen.seat_no:
                log.debug(
                    "runoff_dispatch_skipped_already_pending game=%s seat=%d request=%s",
                    game_id,
                    chosen.seat_no,
                    pending.request_id,
                )
                return

        # Re-entrancy guard #2 (in-flight announcement): phase entry
        # into DAY_RUNOFF_SPEECH fires `try_dispatch_next` from two
        # sequential paths — `_master_narrate` (PHASE_CHANGE narration)
        # and `_on_reactive_phase_enter` (driven by
        # `_dispatch_submissions`). Between the first call's
        # `_runoff_announce` (Levi TTS, several seconds) and
        # `dispatch_request` completing, `_pending` is still empty,
        # so the second call would pick the same seat and re-announce.
        # `_runoff_in_flight` plugs this race window — set BEFORE the
        # await on `_runoff_announce`, cleared in `finally` once the
        # downstream dispatch has handed off to the existing tracking
        # (`_pending` for LLM, `_runoff_human_watchdog` for human).
        in_flight_key = (game_id, day)
        in_flight = self._runoff_in_flight.setdefault(in_flight_key, set())
        if chosen.seat_no in in_flight:
            log.debug(
                "runoff_dispatch_skipped_in_flight game=%s seat=%d",
                game_id,
                chosen.seat_no,
            )
            return
        in_flight.add(chosen.seat_no)

        try:
            # Master narration: name the candidate before they speak.
            # Awaited so the intro finishes before the NPC's own TTS
            # (or the human's grace window) starts.
            if self._runoff_announce is not None:
                try:
                    await self._runoff_announce(chosen)
                except Exception:
                    log.exception(
                        "runoff_announce_failed game=%s seat=%d",
                        game_id,
                        chosen.seat_no,
                    )

            if not chosen.is_llm:
                # Human candidate path: schedule a grace watchdog. The
                # human is expected to speak via voice / text within
                # `runoff_speech_grace` seconds; the SpeechEvent path
                # auto-marks them done (top-of-method scan on the next
                # `try_dispatch_next` invocation). On timeout the
                # watchdog marks them done so the phase doesn't stall
                # on silence.
                self._spawn_human_runoff_watchdog(
                    game_id=game_id,
                    day=day,
                    seat_no=chosen.seat_no,
                )
                return

            # LLM candidate path: rebuild state, dispatch SpeakRequest,
            # spawn watchdog.
            state = await self.rebuild_public_state(
                game_id=game_id, day=day, phase=Phase.DAY_RUNOFF_SPEECH
            )
            if state is None:
                self._maybe_wake_runoff(game_id)
                return

            assert chosen_npc_id is not None  # LLM branch guarantees this
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
            self._spawn_llm_runoff_watchdog(
                game_id=game_id,
                request_id=request.request_id,
                seat_no=chosen.seat_no,
                phase_id=state.phase_id,
            )
        finally:
            in_flight.discard(chosen.seat_no)
            if not in_flight:
                self._runoff_in_flight.pop(in_flight_key, None)

    def _spawn_llm_runoff_watchdog(
        self,
        *,
        game_id: str,
        request_id: str,
        seat_no: int,
        phase_id: str,
    ) -> None:
        """One-shot LLM-runoff watchdog (extracted from `_dispatch_runoff_next`).

        See the inline doc on `_dispatch_runoff_next` for the rationale —
        the body was moved out so the human-grace watchdog
        (`_spawn_human_runoff_watchdog`) can sit alongside it.
        """
        ttl_s = max(1.0, self.config.request_ttl_ms / 1000.0)

        async def _watchdog() -> None:
            try:
                await asyncio.sleep(ttl_s)
            except asyncio.CancelledError:
                return
            if request_id not in self._pending:
                return  # SpeakResult already resolved AND playback finished.
            # SpeakResult acceptance moves the request into _active_playback
            # but does NOT pop _pending (handle_playback_finished does that
            # when playback ends). If playback runs longer than ttl_s
            # (typical NPC TTS is 10-15s vs the 8s TTL), the watchdog wakes
            # while _pending still has the entry — but the NPC HAS responded.
            # Without this check the watchdog spuriously pops _pending,
            # which then makes `_on_playback_finished` lose the game_id
            # lookup → try_dispatch_next is never called → runoff stalls
            # (game d57c5d83ed4a day 2: ジョナス 11.6s playback shadowed
            # the 8s watchdog and シゲミチ was never dispatched).
            if request_id in self._active_playback:
                return  # Accepted; playback handler will re-dispatch on finish.
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
                log.exception("runoff_watchdog_redispatch_failed game=%s", game_id)

        task = asyncio.create_task(_watchdog(), name=f"runoff-watchdog-{request_id}")
        self._runoff_watchdog_tasks.add(task)
        task.add_done_callback(self._runoff_watchdog_tasks.discard)

    def _spawn_human_runoff_watchdog(
        self,
        *,
        game_id: str,
        day: int,
        seat_no: int,
    ) -> None:
        """Grace timer for a human runoff candidate.

        The human is expected to speak (voice or text) within
        ``runoff_speech_grace`` seconds of the Levi intro. If a
        SpeechEvent for this seat lands during the window, the
        top-of-method scan in `_dispatch_runoff_next` flips
        `runoff_speech_done` on the next `try_dispatch_next` invocation.
        On timeout, this watchdog flips the flag itself so
        `plan_runoff_speech_to_runoff` can advance.

        Dedup: keyed on (game_id, day, seat_no) — re-entrant calls
        during the grace window are silently ignored. The strong-ref
        task lives in `_runoff_watchdog_tasks` (shared with the LLM
        watchdogs).
        """
        watchdog_key = (game_id, day)
        scheduled = self._runoff_human_watchdog.setdefault(watchdog_key, set())
        if seat_no in scheduled:
            return
        scheduled.add(seat_no)

        try:
            from wolfbot.domain.durations import current_phase_durations

            grace_s = max(1.0, float(current_phase_durations().runoff_speech_grace))
        except Exception:
            grace_s = 30.0

        async def _watchdog() -> None:
            try:
                await asyncio.sleep(grace_s)
            except asyncio.CancelledError:
                return
            try:
                progress = await self.repo.load_llm_speech_progress(
                    game_id, day, seat_no
                )
            except Exception:
                log.exception(
                    "runoff_human_watchdog_load_progress_failed game=%s seat=%d",
                    game_id,
                    seat_no,
                )
                progress = (0, False, None, 0, False)
            already_done = bool(progress[4])
            scheduled.discard(seat_no)
            if not scheduled:
                self._runoff_human_watchdog.pop(watchdog_key, None)
            if already_done:
                return  # Human spoke within the grace window.
            log.info(
                "runoff_human_grace_timeout game=%s seat=%d — marking done",
                game_id,
                seat_no,
            )
            try:
                await self.repo.mark_llm_runoff_speech_done(game_id, day, seat_no)
            except Exception:
                log.exception(
                    "runoff_human_mark_done_failed game=%s seat=%d",
                    game_id,
                    seat_no,
                )
            self._maybe_wake_runoff(game_id)
            try:
                await self.try_dispatch_next(game_id)
            except Exception:
                log.exception(
                    "runoff_human_watchdog_redispatch_failed game=%s", game_id
                )

        task = asyncio.create_task(
            _watchdog(), name=f"runoff-human-watchdog-{game_id}-d{day}-s{seat_no}"
        )
        self._runoff_watchdog_tasks.add(task)
        task.add_done_callback(self._runoff_watchdog_tasks.discard)

    def _find_npc_for_seat(self, game_id: str, seat_no: int) -> Any | None:
        """Lookup the registry entry pinned to ``(game_id, seat_no)``."""
        for entry in self.registry.all_online():
            if entry.assigned_seat == seat_no and entry.game_id == game_id:
                return entry
        return None

    async def _compute_callout_pool(
        self, game_id: str, state: PublicDiscussionState
    ) -> frozenset[int]:
        """Return seats that should be prioritized when a role callout is
        pending and unanswered, OR when a first-CO of an info role just
        landed and the counter-CO opportunity window is still open.

        Two trigger sources, combined into one pool:

          - ``state.pending_role_callouts`` — public callouts ("占い師は?")
          - ``state.pending_co_response`` — first-CO trigger that opens
            a counter-CO window where every wolf-side seat (and the real
            role-holder when the CO'er was wolf-side) gets one guaranteed
            chance to respond before normal priority resumes.

        Pool composition for any active role:

          - ``"seer"`` / ``"medium"`` / ``"knight"`` — pool = real
            role-holder (alive, uncpd) + every wolf-side seat (人狼/狂人,
            alive, no info CO yet).
          - ``"info_request"`` (callouts only — first-CO doesn't fire
            this) — pool = ALL real info-role holders + wolf-side.

        The arbiter prioritizes pool members (random within pool). Each
        member gets at most one chance via the ``_callout_pool_asked``
        tracker; declines fall through to other pool members until the
        pool is exhausted, after which normal priority resumes.
        """
        if not state.pending_role_callouts and not state.pending_co_response:
            return frozenset()
        callout_to_role = {
            "seer": Role.SEER,
            "medium": Role.MEDIUM,
            "knight": Role.KNIGHT,
        }
        # Resolve the set of real roles that should be in the pool.
        # `info_request` expands to every info role. Both pending sources
        # contribute their role keys symmetrically.
        target_roles: set[Role] = set()
        has_info_request = "info_request" in state.pending_role_callouts
        if has_info_request:
            target_roles |= {Role.SEER, Role.MEDIUM, Role.KNIGHT}
        for callout_key, role in callout_to_role.items():
            if (
                callout_key in state.pending_role_callouts
                or callout_key in state.pending_co_response
            ):
                target_roles.add(role)
        if not target_roles:
            return frozenset()
        try:
            players = await self.repo.load_players(game_id)
            seats = await self.repo.load_seats(game_id)
        except Exception:
            log.exception("callout_pool_load_failed game=%s", game_id)
            return frozenset()
        seats_by_no: dict[int, Seat] = {s.seat_no: s for s in seats}
        co_keys: set[tuple[int, str]] = {(c.seat, c.role_claim) for c in state.co_claims}
        pool: set[int] = set()
        for player in players:
            if not player.alive:
                continue
            if player.seat_no not in state.alive_seat_nos:
                continue
            seat = seats_by_no.get(player.seat_no)
            if seat is None or not seat.is_llm:
                continue
            # Real role-holder for any target role, not yet CO'd as that
            # role. (For info_request, all three info roles are targets.)
            if player.role in target_roles:
                role_callout_key = next(
                    (k for k, r in callout_to_role.items() if r is player.role),
                    None,
                )
                if (
                    role_callout_key is not None
                    and (player.seat_no, role_callout_key) not in co_keys
                ):
                    pool.add(player.seat_no)
            # Wolf-side seats not yet CO'd as any info role can fake any
            # of the requested roles, so prioritize them too.
            if player.role in (Role.WEREWOLF, Role.MADMAN):
                already_co_info = any(
                    (player.seat_no, role) in co_keys for role in ("seer", "medium", "knight")
                )
                if not already_co_info:
                    pool.add(player.seat_no)
        return frozenset(pool)

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
            await self.repo.mark_llm_runoff_speech_done(game_id, day, seat_no)
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
        self._callout_pool_asked.pop(game_id, None)
        # Drop fabrication-retry counters for this game.
        for key in list(self._fabrication_retries.keys()):
            if key[0] == game_id:
                self._fabrication_retries.pop(key, None)
        # Drop fabrication cap-hit blocklist for this game.
        for cap_key in list(self._fabrication_capped.keys()):
            if cap_key[0] == game_id:
                self._fabrication_capped.pop(cap_key, None)
        if swept:
            log.info(
                "speak_arbiter_cleanup_game game=%s swept=%d",
                game_id,
                swept,
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
        # Pull game-wide CO history so the per-phase rebuild's volley-demotion
        # signal (`is_new_co`) treats day-N re-assertions of a day-(N-1) CO
        # as not-new. Without this seed, ジョナス re-CO'ing seer on day 2
        # makes every utterance look like fresh info, suppressing the
        # `_PAIR_VOLLEY_WINDOW` demotion gate (game a701a7531dca day 2).
        # Restricted to events outside the current phase so a seat's
        # FIRST in-phase CO still counts as new info (test_try_dispatch_next
        # _lru_when_speech_counts_tied depends on this).
        try:
            all_events = await self.discussion.load_for_game(game_id)
        except Exception:
            log.exception("co_claim_history_load_failed game=%s", game_id)
            all_events = ()
        prior_phase_events = tuple(e for e in all_events if e.phase_id != phase_id)
        prior_co_claims = (
            extract_co_claims_from_events(prior_phase_events) if prior_phase_events else ()
        )
        prior_co_keys = frozenset((c.seat, c.role_claim) for c in prior_co_claims)
        state = rebuild_public_state_from_events(events, prior_co_keys=prior_co_keys)
        if state is None:
            return None
        # state.co_claims should reflect game-wide CO history (used by NPC
        # prompts on day 2+ to remember day-1 declarations).
        if all_events:
            state.co_claims = extract_co_claims_from_events(all_events)
        return state


__all__ = ["SpeakArbiter", "SpeakArbiterConfig"]


# Force a non-static reference so the linter keeps Awaitable / Callable
# imports for downstream typing extensions.
_ = (Awaitable, Callable)
