"""Export one finished/aborted game to viewer-compatible JSON.

Joins three data sources into one self-contained file the
:mod:`viewer/` Next.js app can read directly:

  1. SQLite — game / seats / public logs / votes / night actions / speech events
  2. ``logs/llm_calls/{game_id}/*.jsonl`` — gameplay + npc_speech + voice_stt traces
  3. (derived) phase grouping, victory inference

The output schema is the canonical
:class:`wolfbot.services.game_export_types.GameExport` (mirrored 1:1 in
``viewer/src/lib/types.ts``). Time fields exposed to the viewer are
uniformly milliseconds since epoch — the DB stores ``created_at`` /
``submitted_at`` in seconds, so this module multiplies by 1000 at the
boundary.

The exporter is invoked in two places:

* :class:`wolfbot.services.game_service.GameService` calls it as a
  ``_on_game_end_finalize`` hook on victory and on host abort.
* ``scripts/export-game.py`` provides a manual CLI for re-exporting any
  past game by id.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import aiosqlite
from pydantic import ValidationError

from wolfbot.services.game_export_types import (
    ArbiterDecisionEntry,
    ClaimedMediumExport,
    ClaimedMediumHistoryEntry,
    ClaimedSeerExport,
    ClaimedSeerHistoryEntry,
    ClaimHistoryEntry,
    DeathCause,
    DiscussionMode,
    GameExport,
    GameMeta,
    NightActionExport,
    PhaseSection,
    PublicLogEntry,
    RoleKey,
    SeatExport,
    SpeechEventExport,
    SpeechSource,
    TraceEntry,
    Victory,
    VoteExport,
    WolfChatLogEntry,
)

log = logging.getLogger(__name__)

# Default output dir — the viewer auto-discovers files here, so finishing
# a game and running ``cd viewer && pnpm dev`` Just Works with no flags.
_DEFAULT_OUTPUT_DIR = Path("viewer/games")
# Default trace dir — must match wolfbot.services.llm_trace's default.
_DEFAULT_TRACE_DIR = Path("logs/llm_calls")


async def export_game(
    *,
    game_id: str,
    db_path: Path | str,
    trace_dir: Path | str | None = None,
    output_dir: Path | str | None = None,
) -> Path:
    """Build the viewer JSON for ``game_id`` and write it to disk.

    Returns the absolute path of the written file. Raises on any DB error,
    missing-game, or schema-violating data; caller decides whether to
    swallow or propagate.
    """
    db_path = Path(db_path)
    trace_root = Path(trace_dir) if trace_dir else _DEFAULT_TRACE_DIR
    out_dir = Path(output_dir) if output_dir else _DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = await _build_payload(game_id, db_path, trace_root)
    # Filename is timestamp-prefixed (sortable / human-readable) instead
    # of the random game_id. Local-time format YYYY-MM-DD_HH-MM-SS comes
    # from the game's created_at — the viewer auto-discovers files by
    # mtime, so the prefix only matters for human listing. If a same-
    # second collision happens, append _<n> to keep the older file.
    ts = datetime.fromtimestamp(payload.game.created_at_ms / 1000)
    base_name = ts.strftime("%Y-%m-%d_%H-%M-%S")
    out_path = out_dir / f"{base_name}.json"
    suffix = 1
    while out_path.exists() and out_path.stat().st_size > 0:
        # Different game wrote the same-second filename earlier — disambiguate.
        existing = out_path.read_text(encoding="utf-8")
        if f'"id": "{game_id}"' in existing:
            # Same game re-export — overwrite.
            break
        out_path = out_dir / f"{base_name}_{suffix}.json"
        suffix += 1
    out_path.write_text(
        payload.model_dump_json(indent=2),
        encoding="utf-8",
    )
    log.info(
        "game_exported game=%s path=%s phases=%d trace_lines=%d arbiter_decisions=%d",
        game_id,
        out_path,
        len(payload.phases),
        len(payload.trace),
        len(payload.arbiter_decisions),
    )
    return out_path.resolve()


async def _build_payload(game_id: str, db_path: Path, trace_root: Path) -> GameExport:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        game_row = await _fetch_one(
            db,
            "SELECT * FROM games WHERE id = ?",
            (game_id,),
        )
        if game_row is None:
            raise ValueError(f"game not found: {game_id}")

        seat_rows = [
            dict(r)
            for r in await _fetch_all(
                db,
                "SELECT * FROM seats WHERE game_id = ? ORDER BY seat_no",
                (game_id,),
            )
        ]
        # PLAYER_SPEECH log rows duplicate the canonical speech_events rows
        # (DiscussionService.record() inserts both at write time so live LLM
        # context-builder prompts can keep reading from logs_public). For
        # replay we only need the speech_events row — it carries source +
        # speaker_seat + summary, which the viewer attributes to the player.
        # Excluding the duplicates here keeps the timeline clean and avoids
        # the "NPC text shown twice (PLAYER_SPEECH log + speech_event)" issue.
        public_log_rows = [
            dict(r)
            for r in await _fetch_all(
                db,
                "SELECT day, phase, kind, actor_seat, text, created_at "
                "FROM logs_public WHERE game_id = ? AND kind != 'PLAYER_SPEECH' "
                "ORDER BY id ASC",
                (game_id,),
            )
        ]
        vote_rows = [
            dict(r)
            for r in await _fetch_all(
                db,
                "SELECT day, round, voter_seat, target_seat, submitted_at "
                "FROM votes WHERE game_id = ? "
                "ORDER BY day, round, submitted_at",
                (game_id,),
            )
        ]
        night_action_rows = [
            dict(r)
            for r in await _fetch_all(
                db,
                "SELECT day, actor_seat, kind, target_seat, submitted_at "
                "FROM night_actions WHERE game_id = ? "
                "ORDER BY day, submitted_at",
                (game_id,),
            )
        ]
        # Wolf chat is fan-out-stored in `logs_private` (one row per
        # audience seat) so the same utterance shows up N times where N
        # = alive wolves at the moment. Dedupe on
        # (actor_seat, created_at, text) keeps one row per actual
        # utterance — that's what the viewer wants to render. Phase is
        # always 'NIGHT' (or 'NIGHT_0' on day 0) by construction; we
        # carry it through so the per-phase grouping in `_build_phases`
        # picks it up without extra logic.
        wolf_chat_rows = [
            dict(r)
            for r in await _fetch_all(
                db,
                "SELECT DISTINCT day, phase, actor_seat, text, created_at "
                "FROM logs_private "
                "WHERE game_id = ? AND kind = 'WOLF_CHAT' AND actor_seat IS NOT NULL "
                "ORDER BY created_at ASC",
                (game_id,),
            )
        ]
        # `phase_baseline` rows are an internal sentinel used by
        # PublicDiscussionState to seed alive-seat baselines; they have
        # empty text and are explicitly excluded from public-log emission
        # in the live system. Filter at the SQL level so the viewer never
        # sees them.
        speech_event_rows = [
            dict(r)
            for r in await _fetch_all(
                db,
                "SELECT event_id, day, phase, source, speaker_seat, text, "
                "stt_confidence, summary, co_declaration, addressed_seat_no, "
                "claimed_seer_target_seat, claimed_seer_is_wolf, "
                "claimed_medium_target_seat, claimed_medium_is_wolf, "
                "created_at_ms "
                "FROM speech_events WHERE game_id = ? "
                "AND source != 'phase_baseline' "
                "ORDER BY created_at_ms ASC",
                (game_id,),
            )
        ]
        # Arbiter decision timeline — joined LEFT-OUTER from requests so
        # in-flight or rejected dispatches still appear (results /
        # playback may legitimately be missing).
        arbiter_rows = [
            dict(r)
            for r in await _fetch_all(
                db,
                """
            SELECT
                req.request_id, req.phase_id, req.npc_id, req.seat_no,
                req.suggested_intent, req.selection_reason,
                req.public_state_snapshot_json, req.logic_packet_id,
                req.created_at_ms, req.expires_at_ms,
                res.status AS result_status,
                res.text AS result_text,
                res.intent AS result_intent,
                res.failure_reason AS result_failure_reason,
                res.received_at_ms AS result_received_at_ms,
                pb.outcome AS playback_outcome,
                pb.failure_reason AS playback_failure_reason,
                pb.finished_at_ms AS playback_finished_at_ms,
                pb.tts_outcome AS tts_outcome,
                pb.tts_duration_ms AS tts_duration_ms
            FROM npc_speak_requests req
            LEFT JOIN npc_speak_results res ON res.request_id = req.request_id
            LEFT JOIN npc_playback_events pb ON pb.request_id = req.request_id
            WHERE req.game_id = ?
            ORDER BY req.created_at_ms ASC
            """,
                (game_id,),
            )
        ]

    seat_lookup = {s["seat_no"]: s["display_name"] for s in seat_rows}
    return GameExport(
        game=_build_game_meta(game_row, public_log_rows),
        seats=[_build_seat(s) for s in seat_rows],
        phases=_build_phases(
            _retag_morning_logs_from_text(public_log_rows),
            speech_event_rows,
            vote_rows,
            night_action_rows,
            wolf_chat_rows,
        ),
        trace=_load_trace(trace_root, game_id),
        arbiter_decisions=[_build_arbiter_decision(r) for r in arbiter_rows],
        claim_history=_build_claim_history(speech_event_rows, seat_lookup),
    )


# ---------------------------------------------------------------- DB helpers
async def _fetch_one(
    db: aiosqlite.Connection, sql: str, params: tuple[Any, ...]
) -> aiosqlite.Row | None:
    async with db.execute(sql, params) as cur:
        return await cur.fetchone()


async def _fetch_all(
    db: aiosqlite.Connection, sql: str, params: tuple[Any, ...]
) -> list[aiosqlite.Row]:
    async with db.execute(sql, params) as cur:
        return list(await cur.fetchall())


# ---------------------------------------------------------------- shape builders
def _build_game_meta(row: aiosqlite.Row, public_logs: list[dict[str, Any]]) -> GameMeta:
    discussion_mode = cast(DiscussionMode, row["discussion_mode"])
    return GameMeta(
        id=row["id"],
        guild_id=row["guild_id"],
        host_user_id=row["host_user_id"],
        discussion_mode=discussion_mode,
        created_at_ms=_epoch_to_ms(row["created_at"]),
        ended_at_ms=_epoch_to_ms(row["ended_at"]) if row["ended_at"] else None,
        victory=_infer_victory(public_logs),
        main_text_channel_id=row["main_text_channel_id"],
        main_vc_channel_id=row["main_vc_channel_id"],
    )


def _build_arbiter_decision(row: dict[str, Any]) -> ArbiterDecisionEntry:
    """Build one ArbiterDecisionEntry from the joined query row.

    Older games (pre-selection_reason migration) have NULL in both
    ``selection_reason`` and ``public_state_snapshot_json``; we surface
    them as ``None`` rather than back-fill heuristics so the viewer can
    distinguish "we don't know why" from "we know it was X".
    """
    raw_snapshot = row.get("public_state_snapshot_json")
    snapshot: dict[str, Any] | None = None
    if raw_snapshot:
        try:
            parsed = json.loads(raw_snapshot)
        except json.JSONDecodeError:
            log.warning(
                "skipping malformed public_state_snapshot_json for request %s",
                row.get("request_id"),
            )
            parsed = None
        if isinstance(parsed, dict):
            snapshot = parsed
    return ArbiterDecisionEntry(
        request_id=row["request_id"],
        phase_id=row["phase_id"],
        npc_id=row["npc_id"],
        seat_no=row["seat_no"],
        suggested_intent=row["suggested_intent"],
        selection_reason=row.get("selection_reason"),
        public_state_snapshot=snapshot,
        logic_packet_id=row["logic_packet_id"],
        created_at_ms=row["created_at_ms"],
        expires_at_ms=row["expires_at_ms"],
        result_status=row.get("result_status"),
        result_text=row.get("result_text"),
        result_intent=row.get("result_intent"),
        result_failure_reason=row.get("result_failure_reason"),
        result_received_at_ms=row.get("result_received_at_ms"),
        playback_outcome=row.get("playback_outcome"),
        playback_failure_reason=row.get("playback_failure_reason"),
        playback_finished_at_ms=row.get("playback_finished_at_ms"),
        tts_outcome=row.get("tts_outcome"),
        tts_duration_ms=row.get("tts_duration_ms"),
    )


def _build_seat(row: dict[str, Any]) -> SeatExport:
    role = cast(RoleKey, row["role"] or "VILLAGER")
    death_cause: DeathCause | None = (
        cast(DeathCause, row["death_cause"]) if row["death_cause"] else None
    )
    return SeatExport(
        seat_no=row["seat_no"],
        display_name=row["display_name"],
        is_llm=bool(row["is_llm"]),
        persona_key=row["persona_key"],
        discord_user_id=row["discord_user_id"],
        role=role,
        alive=bool(row["alive"]),
        death_cause=death_cause,
        death_day=row["death_day"],
    )


_DAY_START_TEXT_PATTERN = re.compile(r"^(?:夜が明けました。)?(\d+) 日目の議論を開始")


def _retag_morning_logs_from_text(
    public_logs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Use the day number embedded in PHASE_CHANGE / MORNING text as the
    source of truth for those logs' ``day`` field.

    Pre-2026-05-02 (commit ``cab2b92``), ``plan_night_resolve`` and
    ``plan_night0`` emitted the next-day MORNING + "N 日目の議論を開始"
    PHASE_CHANGE through ``_public_log`` which defaulted ``day =
    game.day_number`` — i.e. the *prior* day at the moment of the
    transition. So a NIGHT 1 → DAY 2 resolve wrote those logs with
    ``day=1``. The viewer then bucketed day-2's morning into day-1's
    DAY_DISCUSSION section, and the timeline read out of order.

    State machine now tags these logs explicitly with the next day, so
    new games store correct values. But ~80 already-played games carry
    the old offset in their DB rows. Reading the day number out of the
    PHASE_CHANGE text ("2 日目の議論を開始") gives us a deterministic
    fix that works for both eras: post-fix games are already aligned
    (no-op), pre-fix games get rewritten on the fly without a DB
    migration.

    MORNING rows ("朝になりました。犠牲者: 〜" / "平和な朝です") have
    no day in their text. They are emitted in the same Transition as
    the day-start PHASE_CHANGE and share its ``created_at`` epoch
    second; we look up the sibling PHASE_CHANGE within the same
    second and copy its corrected day. If no sibling is found (game
    aborted mid-resolution before PHASE_CHANGE landed) the row is
    passed through unchanged.

    The earlier ``_rebucket_morning_logs`` helper indiscriminately did
    ``day += 1`` on every MORNING/PHASE_CHANGE row — that broke
    post-fix games where the day was already correct. Trust the text,
    not a blanket offset.
    """
    # First pass: build a map of (created_at_epoch_s) → corrected_day,
    # populated from PHASE_CHANGE rows whose text encodes the day.
    corrected_day_by_ts: dict[int, int] = {}
    for row in public_logs:
        if row.get("phase") != "DAY_DISCUSSION" or row.get("kind") != "PHASE_CHANGE":
            continue
        text = row.get("text")
        if not isinstance(text, str):
            continue
        m = _DAY_START_TEXT_PATTERN.match(text)
        if m is None:
            continue
        corrected_day_by_ts[int(row["created_at"])] = int(m.group(1))

    fixed: list[dict[str, Any]] = []
    for row in public_logs:
        if row.get("phase") != "DAY_DISCUSSION":
            fixed.append(row)
            continue
        kind = row.get("kind")
        if kind == "PHASE_CHANGE":
            text = row.get("text")
            if isinstance(text, str):
                m = _DAY_START_TEXT_PATTERN.match(text)
                if m is not None:
                    expected = int(m.group(1))
                    if row.get("day") != expected:
                        patched = dict(row)
                        patched["day"] = expected
                        fixed.append(patched)
                        continue
        elif kind == "MORNING":
            sibling_day = corrected_day_by_ts.get(int(row["created_at"]))
            if sibling_day is not None and row.get("day") != sibling_day:
                patched = dict(row)
                patched["day"] = sibling_day
                fixed.append(patched)
                continue
        fixed.append(row)
    return fixed


def _build_phases(
    public_logs: list[dict[str, Any]],
    speech_events: list[dict[str, Any]],
    votes: list[dict[str, Any]],
    night_actions: list[dict[str, Any]],
    wolf_chat_logs: list[dict[str, Any]],
) -> list[PhaseSection]:
    """Group all per-game events into ordered ``(day, phase)`` buckets.

    A phase appears in the output if AT LEAST ONE of public_logs /
    speech_events / votes / night_actions / wolf_chat_logs has data for
    it. We don't invent empty buckets — a game that died in lobby
    produces only a SETUP / LOBBY section, not a full ladder of empty
    NIGHT/DAY rows.
    """
    # Discover (day, phase) pairs in chronological order. Each bucket's
    # ``first_ts_ms`` is the earliest timestamp of any event tagged for
    # it — public_logs (epoch s), speech_events (ms), votes (s),
    # night_actions (s), wolf_chat_logs (s). We track milliseconds so a
    # phase that ends and the next phase that begins within the same
    # epoch second still order correctly (only speech_events carry
    # sub-second resolution today, but a per-(day, phase) tie still
    # falls back to the secondary phase-ordering key below).
    seen: dict[tuple[int, str], int] = {}  # (day, phase) -> first_ts_ms

    def _record(key: tuple[int, str], ts_ms: int) -> None:
        existing = seen.get(key)
        if existing is None or ts_ms < existing:
            seen[key] = ts_ms

    for row in public_logs:
        _record((row["day"], row["phase"]), _epoch_to_ms(row["created_at"]))
    for ev in speech_events:
        _record((ev["day"], ev["phase"]), int(ev["created_at_ms"]))
    for na in night_actions:
        phase = "NIGHT_0" if na["day"] == 0 else "NIGHT"
        _record((na["day"], phase), _epoch_to_ms(na["submitted_at"]))
    for v in votes:
        phase = "DAY_RUNOFF" if v["round"] >= 2 else "DAY_VOTE"
        _record((v["day"], phase), _epoch_to_ms(v["submitted_at"]))
    # Wolf chat surfaces phases that have no other events (rare:
    # attack-less NIGHT where wolves coordinated but never committed).
    for wc in wolf_chat_logs:
        _record((wc["day"], wc["phase"]), _epoch_to_ms(wc["created_at"]))

    # Stable secondary sort: when two phases share a millisecond
    # (typical at the NIGHT-resolve / next-day-MORNING boundary, where
    # the night's last submission and the morning log share an epoch
    # second), enforce within-day phase order so the timeline reads
    # SETUP → DAY_DISCUSSION → DAY_VOTE → DAY_RUNOFF_SPEECH →
    # DAY_RUNOFF → NIGHT → (next day) DAY_DISCUSSION → ...
    phase_order_within_day = {
        "SETUP": 0,
        "NIGHT_0": 1,
        "DAY_DISCUSSION": 2,
        "DAY_VOTE": 3,
        "DAY_RUNOFF_SPEECH": 4,
        "DAY_RUNOFF": 5,
        "NIGHT": 6,
        "GAME_OVER": 7,
        "WAITING_HOST_DECISION": 8,
    }

    def _sort_key(item: tuple[tuple[int, str], int]) -> tuple[int, int, int]:
        (day, phase), first_ts_ms = item
        return (first_ts_ms, day, phase_order_within_day.get(phase, 99))

    ordered = sorted(seen.items(), key=_sort_key)
    result: list[PhaseSection] = []
    for (day, phase), first_ts_ms in ordered:
        result.append(
            PhaseSection(
                day=day,
                phase=phase,
                started_at_ms=first_ts_ms,
                public_logs=[
                    PublicLogEntry(
                        kind=r["kind"],
                        actor_seat=r["actor_seat"],
                        text=r["text"],
                        created_at_ms=_epoch_to_ms(r["created_at"]),
                    )
                    for r in public_logs
                    if r["day"] == day and r["phase"] == phase
                ],
                speech_events=[
                    SpeechEventExport(
                        event_id=ev["event_id"],
                        source=cast(SpeechSource, ev["source"]),
                        speaker_seat=ev["speaker_seat"],
                        text=ev["text"],
                        stt_confidence=ev["stt_confidence"],
                        summary=ev["summary"],
                        co_declaration=ev["co_declaration"],
                        addressed_seat_no=ev["addressed_seat_no"],
                        claimed_seer_result=_build_claimed_seer(ev),
                        claimed_medium_result=_build_claimed_medium(ev),
                        created_at_ms=ev["created_at_ms"],
                    )
                    for ev in speech_events
                    if ev["day"] == day and ev["phase"] == phase
                ],
                votes=[
                    VoteExport(
                        day=v["day"],
                        round=v["round"],
                        voter_seat=v["voter_seat"],
                        target_seat=v["target_seat"],
                        submitted_at_ms=_epoch_to_ms(v["submitted_at"]),
                    )
                    for v in votes
                    if v["day"] == day and _vote_phase(v["round"]) == phase
                ],
                night_actions=[
                    NightActionExport(
                        day=na["day"],
                        actor_seat=na["actor_seat"],
                        kind=na["kind"],
                        target_seat=na["target_seat"],
                        submitted_at_ms=_epoch_to_ms(na["submitted_at"]),
                    )
                    for na in night_actions
                    if na["day"] == day and _night_phase(na["day"]) == phase
                ],
                wolf_chat_logs=[
                    WolfChatLogEntry(
                        actor_seat=wc["actor_seat"],
                        text=wc["text"],
                        created_at_ms=_epoch_to_ms(wc["created_at"]),
                    )
                    for wc in wolf_chat_logs
                    if wc["day"] == day and wc["phase"] == phase
                ],
            )
        )
    return result


def _vote_phase(round_: int) -> str:
    # Schema convention: round=0 is the regular vote, round=1 is the runoff.
    # Anything ≥1 is treated as runoff so the exporter is forward-compatible
    # if a future schema adds a second runoff round.
    return "DAY_RUNOFF" if round_ >= 1 else "DAY_VOTE"


def _night_phase(day: int) -> str:
    return "NIGHT_0" if day == 0 else "NIGHT"


def _infer_victory(public_logs: list[dict[str, Any]]) -> Victory | None:
    for row in reversed(public_logs):
        if row["kind"] != "VICTORY":
            continue
        text = row["text"] or ""
        if "村人" in text or "村陣営" in text:
            return "village"
        if "人狼" in text or "狼陣営" in text:
            return "wolf"
    return None


def _build_claimed_seer(ev: dict[str, Any]) -> ClaimedSeerExport | None:
    """Lift the persisted seer-claim columns into the export model.

    Requires both ``target_seat`` and ``is_wolf`` to be set. SQLite
    stores the verdict as 0/1; the export coerces back to bool here.
    """
    target = ev.get("claimed_seer_target_seat")
    verdict = ev.get("claimed_seer_is_wolf")
    if target is None or verdict is None:
        return None
    return ClaimedSeerExport(target_seat=int(target), is_wolf=bool(verdict))


def _build_claimed_medium(ev: dict[str, Any]) -> ClaimedMediumExport | None:
    """Lift the persisted medium-claim columns into the export model.

    Mirror of :func:`_build_claimed_seer` but ``is_wolf`` may be NULL
    to encode "no execution yesterday → no result today" — preserved
    as ``None`` so the viewer can render the void.
    """
    target = ev.get("claimed_medium_target_seat")
    if target is None:
        return None
    raw_verdict = ev.get("claimed_medium_is_wolf")
    verdict = bool(raw_verdict) if raw_verdict is not None else None
    return ClaimedMediumExport(target_seat=int(target), is_wolf=verdict)


def _build_claim_history(
    speech_events: list[dict[str, Any]],
    seat_lookup: dict[int, str],
) -> list[ClaimHistoryEntry]:
    """Fold the per-event claim columns into a per-claimer ledger.

    Pure projection: every speech_events row already carries the
    ``claimed_*`` columns straight from the DB; here we group them by
    speaker_seat and sort each group chronologically. The result is
    keyed implicitly by ``claimer_seat`` (each entry carries the seat
    number) and sorted ascending so the viewer renders a stable list.
    """
    seer_by_seat: dict[int, list[ClaimedSeerHistoryEntry]] = {}
    medium_by_seat: dict[int, list[ClaimedMediumHistoryEntry]] = {}

    def _name(seat: int) -> str:
        return seat_lookup.get(seat) or f"席{seat}"

    for ev in speech_events:
        speaker = ev.get("speaker_seat")
        if speaker is None:
            continue
        speaker_seat = int(speaker)
        seer_target = ev.get("claimed_seer_target_seat")
        seer_verdict = ev.get("claimed_seer_is_wolf")
        if seer_target is not None and seer_verdict is not None:
            seer_by_seat.setdefault(speaker_seat, []).append(
                ClaimedSeerHistoryEntry(
                    day=int(ev["day"]),
                    target_seat=int(seer_target),
                    target_name=_name(int(seer_target)),
                    is_wolf=bool(seer_verdict),
                    declared_at_event_id=ev["event_id"],
                )
            )
        medium_target = ev.get("claimed_medium_target_seat")
        if medium_target is not None:
            raw_verdict = ev.get("claimed_medium_is_wolf")
            verdict = bool(raw_verdict) if raw_verdict is not None else None
            medium_by_seat.setdefault(speaker_seat, []).append(
                ClaimedMediumHistoryEntry(
                    day=int(ev["day"]),
                    target_seat=int(medium_target),
                    target_name=_name(int(medium_target)),
                    is_wolf=verdict,
                    declared_at_event_id=ev["event_id"],
                )
            )

    seats = sorted(set(seer_by_seat) | set(medium_by_seat))
    return [
        ClaimHistoryEntry(
            claimer_seat=seat,
            seer_claims=seer_by_seat.get(seat, []),
            medium_claims=medium_by_seat.get(seat, []),
        )
        for seat in seats
    ]


def _epoch_to_ms(epoch_seconds: int | None) -> int:
    """Promote an epoch-second timestamp into the viewer's ms unit.

    DB columns ``created_at`` / ``submitted_at`` are seconds; the viewer
    schema is uniform milliseconds. Returns 0 for None to keep the JSON
    well-typed (callers usually pass non-null ts at this point).
    """
    if epoch_seconds is None:
        return 0
    return int(epoch_seconds) * 1000


# ---------------------------------------------------------------- trace loader
def _load_trace(trace_root: Path, game_id: str) -> list[TraceEntry]:
    """Walk ``logs/llm_calls/{game_id}/*.jsonl`` and inline every entry.

    Missing dir = empty list (game ran with trace disabled, or pre-trace
    games). One bad line is logged and skipped — not fatal. A schema-
    violating line (missing required field, wrong type) is also logged
    and skipped rather than failing the whole export.

    Backfill: pre-fix voice_stt / NPC trace lines were written with
    ``day=null`` (the call sites passed only ``phase=phase_id`` to
    ``trace_context``). The viewer's ``matchTraceForSpeech`` requires
    ``t.day === phase.day`` to attach a trace to a speech event, so
    null-day lines never surfaced the LLM prompt UI. We recover the day
    here by parsing ``dayN`` out of the canonical phase_id format
    ``"{gid}::dayN::PHASE::seq"`` whenever ``day`` is missing.
    """
    from wolfbot.services.llm_trace import parse_day_from_phase_id

    game_dir = trace_root / game_id
    if not game_dir.is_dir():
        return []
    entries: list[TraceEntry] = []
    for jsonl_path in sorted(game_dir.glob("*.jsonl")):
        stem = jsonl_path.stem  # gameplay / npc_setsu / voice_stt / ...
        try:
            for raw_line in jsonl_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("skipping malformed trace line in %s", jsonl_path)
                    continue
                obj.setdefault("file_stem", stem)
                if obj.get("day") is None:
                    recovered = parse_day_from_phase_id(obj.get("phase"))
                    if recovered is not None:
                        obj["day"] = recovered
                try:
                    entries.append(TraceEntry.model_validate(obj))
                except ValidationError:
                    log.exception(
                        "skipping schema-violating trace line in %s",
                        jsonl_path,
                    )
        except OSError:
            log.exception("could not read trace file %s", jsonl_path)
    # Sort by ts when present so the flat list reads chronologically.
    entries.sort(key=lambda e: e.ts or "")
    return entries


__all__ = ["export_game"]
