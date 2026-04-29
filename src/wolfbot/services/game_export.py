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
from pathlib import Path
from typing import Any, cast

import aiosqlite
from pydantic import ValidationError

from wolfbot.services.game_export_types import (
    ArbiterDecisionEntry,
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
    out_path = out_dir / f"{game_id}.json"
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


async def _build_payload(
    game_id: str, db_path: Path, trace_root: Path
) -> GameExport:
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

        seat_rows = [dict(r) for r in await _fetch_all(
            db,
            "SELECT * FROM seats WHERE game_id = ? ORDER BY seat_no",
            (game_id,),
        )]
        # PLAYER_SPEECH log rows duplicate the canonical speech_events rows
        # (DiscussionService.record() inserts both at write time so live LLM
        # context-builder prompts can keep reading from logs_public). For
        # replay we only need the speech_events row — it carries source +
        # speaker_seat + summary, which the viewer attributes to the player.
        # Excluding the duplicates here keeps the timeline clean and avoids
        # the "NPC text shown twice (PLAYER_SPEECH log + speech_event)" issue.
        public_log_rows = [dict(r) for r in await _fetch_all(
            db,
            "SELECT day, phase, kind, actor_seat, text, created_at "
            "FROM logs_public WHERE game_id = ? AND kind != 'PLAYER_SPEECH' "
            "ORDER BY id ASC",
            (game_id,),
        )]
        vote_rows = [dict(r) for r in await _fetch_all(
            db,
            "SELECT day, round, voter_seat, target_seat, submitted_at "
            "FROM votes WHERE game_id = ? "
            "ORDER BY day, round, submitted_at",
            (game_id,),
        )]
        night_action_rows = [dict(r) for r in await _fetch_all(
            db,
            "SELECT day, actor_seat, kind, target_seat, submitted_at "
            "FROM night_actions WHERE game_id = ? "
            "ORDER BY day, submitted_at",
            (game_id,),
        )]
        # `phase_baseline` rows are an internal sentinel used by
        # PublicDiscussionState to seed alive-seat baselines; they have
        # empty text and are explicitly excluded from public-log emission
        # in the live system. Filter at the SQL level so the viewer never
        # sees them.
        speech_event_rows = [dict(r) for r in await _fetch_all(
            db,
            "SELECT event_id, day, phase, source, speaker_seat, text, "
            "stt_confidence, summary, co_declaration, addressed_seat_no, "
            "created_at_ms "
            "FROM speech_events WHERE game_id = ? "
            "AND source != 'phase_baseline' "
            "ORDER BY created_at_ms ASC",
            (game_id,),
        )]
        # Arbiter decision timeline — joined LEFT-OUTER from requests so
        # in-flight or rejected dispatches still appear (results /
        # playback may legitimately be missing).
        arbiter_rows = [dict(r) for r in await _fetch_all(
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
        )]

    return GameExport(
        game=_build_game_meta(game_row, public_log_rows),
        seats=[_build_seat(s) for s in seat_rows],
        phases=_build_phases(
            public_log_rows, speech_event_rows, vote_rows, night_action_rows
        ),
        trace=_load_trace(trace_root, game_id),
        arbiter_decisions=[_build_arbiter_decision(r) for r in arbiter_rows],
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
def _build_game_meta(
    row: aiosqlite.Row, public_logs: list[dict[str, Any]]
) -> GameMeta:
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


def _build_phases(
    public_logs: list[dict[str, Any]],
    speech_events: list[dict[str, Any]],
    votes: list[dict[str, Any]],
    night_actions: list[dict[str, Any]],
) -> list[PhaseSection]:
    """Group all per-game events into ordered ``(day, phase)`` buckets.

    A phase appears in the output if AT LEAST ONE of public_logs /
    speech_events / votes / night_actions has data for it. We don't
    invent empty buckets — a game that died in lobby produces only a
    SETUP / LOBBY section, not a full ladder of empty NIGHT/DAY rows.
    """
    # Discover (day, phase) pairs in chronological order from public logs;
    # those are the most reliable per-phase event source.
    seen: dict[tuple[int, str], int] = {}  # (day, phase) -> first_ts (epoch s)
    for row in public_logs:
        key = (row["day"], row["phase"])
        if key not in seen:
            seen[key] = row["created_at"]
    # Plus any phase that has speeches but no public log:
    for ev in speech_events:
        key = (ev["day"], ev["phase"])
        if key not in seen:
            seen[key] = ev["created_at_ms"] // 1000
    # Plus night phases inferred from night_actions:
    for na in night_actions:
        phase = "NIGHT_0" if na["day"] == 0 else "NIGHT"
        key = (na["day"], phase)
        if key not in seen:
            seen[key] = na["submitted_at"]
    # Plus vote phases inferred from votes:
    for v in votes:
        phase = "DAY_RUNOFF" if v["round"] >= 2 else "DAY_VOTE"
        key = (v["day"], phase)
        if key not in seen:
            seen[key] = v["submitted_at"]

    ordered = sorted(seen.items(), key=lambda kv: kv[1])
    result: list[PhaseSection] = []
    for (day, phase), first_ts in ordered:
        result.append(
            PhaseSection(
                day=day,
                phase=phase,
                started_at_ms=_epoch_to_ms(first_ts),
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
                    log.warning(
                        "skipping malformed trace line in %s", jsonl_path
                    )
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
