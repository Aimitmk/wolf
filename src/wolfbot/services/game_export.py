"""Export one finished/aborted game to viewer-compatible JSON.

Joins three data sources into one self-contained file the
:mod:`viewer/` Next.js app can read directly:

  1. SQLite — game / seats / public logs / votes / night actions / speech events
  2. ``logs/llm_calls/{game_id}/*.jsonl`` — gameplay + npc_speech + voice_stt traces
  3. (derived) phase grouping, victory inference

The output schema is the canonical
:class:`viewer/src/lib/types.ts::GameSample`. Time fields exposed to the
viewer are uniformly milliseconds since epoch — the DB stores
``created_at`` / ``submitted_at`` in seconds, so this module multiplies
by 1000 at the boundary.

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
from typing import Any

import aiosqlite

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

    Returns the absolute path of the written file. Raises on any DB error
    or missing-game; caller decides whether to swallow or propagate.
    """
    db_path = Path(db_path)
    trace_root = Path(trace_dir) if trace_dir else _DEFAULT_TRACE_DIR
    out_dir = Path(output_dir) if output_dir else _DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = await _build_payload(game_id, db_path, trace_root)
    out_path = out_dir / f"{game_id}.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(
        "game_exported game=%s path=%s phases=%d trace_lines=%d",
        game_id,
        out_path,
        len(payload["phases"]),
        len(payload["trace"]),
    )
    return out_path.resolve()


async def _build_payload(
    game_id: str, db_path: Path, trace_root: Path
) -> dict[str, Any]:
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

        seats = [dict(r) for r in await _fetch_all(
            db,
            "SELECT * FROM seats WHERE game_id = ? ORDER BY seat_no",
            (game_id,),
        )]
        public_logs = [dict(r) for r in await _fetch_all(
            db,
            "SELECT day, phase, kind, actor_seat, text, created_at "
            "FROM logs_public WHERE game_id = ? ORDER BY id ASC",
            (game_id,),
        )]
        votes = [dict(r) for r in await _fetch_all(
            db,
            "SELECT day, round, voter_seat, target_seat, submitted_at "
            "FROM votes WHERE game_id = ? "
            "ORDER BY day, round, submitted_at",
            (game_id,),
        )]
        night_actions = [dict(r) for r in await _fetch_all(
            db,
            "SELECT day, actor_seat, kind, target_seat, submitted_at "
            "FROM night_actions WHERE game_id = ? "
            "ORDER BY day, submitted_at",
            (game_id,),
        )]
        speech_events = [dict(r) for r in await _fetch_all(
            db,
            "SELECT event_id, day, phase, source, speaker_seat, text, "
            "stt_confidence, summary, co_declaration, addressed_seat_no, "
            "created_at_ms "
            "FROM speech_events WHERE game_id = ? ORDER BY created_at_ms ASC",
            (game_id,),
        )]

    return {
        "game": _build_game_meta(game_row, public_logs),
        "seats": [_build_seat(s) for s in seats],
        "phases": _build_phases(
            public_logs, speech_events, votes, night_actions
        ),
        "trace": _load_trace(trace_root, game_id),
    }


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
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "guild_id": row["guild_id"],
        "host_user_id": row["host_user_id"],
        "discussion_mode": row["discussion_mode"],
        "created_at_ms": _epoch_to_ms(row["created_at"]),
        "ended_at_ms": _epoch_to_ms(row["ended_at"]) if row["ended_at"] else None,
        "victory": _infer_victory(public_logs),
        "main_text_channel_id": row["main_text_channel_id"],
        "main_vc_channel_id": row["main_vc_channel_id"],
    }


def _build_seat(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "seat_no": row["seat_no"],
        "display_name": row["display_name"],
        "is_llm": bool(row["is_llm"]),
        "persona_key": row["persona_key"],
        "discord_user_id": row["discord_user_id"],
        "role": row["role"] or "VILLAGER",
        "alive": bool(row["alive"]),
        "death_cause": row["death_cause"],
        "death_day": row["death_day"],
    }


def _build_phases(
    public_logs: list[dict[str, Any]],
    speech_events: list[dict[str, Any]],
    votes: list[dict[str, Any]],
    night_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
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
    result: list[dict[str, Any]] = []
    for (day, phase), first_ts in ordered:
        result.append(
            {
                "day": day,
                "phase": phase,
                "started_at_ms": _epoch_to_ms(first_ts),
                "public_logs": [
                    {
                        "kind": r["kind"],
                        "actor_seat": r["actor_seat"],
                        "text": r["text"],
                        "created_at_ms": _epoch_to_ms(r["created_at"]),
                    }
                    for r in public_logs
                    if r["day"] == day and r["phase"] == phase
                ],
                "speech_events": [
                    {
                        "event_id": ev["event_id"],
                        "source": ev["source"],
                        "speaker_seat": ev["speaker_seat"],
                        "text": ev["text"],
                        "stt_confidence": ev["stt_confidence"],
                        "summary": ev["summary"],
                        "co_declaration": ev["co_declaration"],
                        "addressed_seat_no": ev["addressed_seat_no"],
                        "created_at_ms": ev["created_at_ms"],
                    }
                    for ev in speech_events
                    if ev["day"] == day and ev["phase"] == phase
                ],
                "votes": [
                    {
                        "day": v["day"],
                        "round": v["round"],
                        "voter_seat": v["voter_seat"],
                        "target_seat": v["target_seat"],
                        "submitted_at_ms": _epoch_to_ms(v["submitted_at"]),
                    }
                    for v in votes
                    if v["day"] == day and _vote_phase(v["round"]) == phase
                ],
                "night_actions": [
                    {
                        "day": na["day"],
                        "actor_seat": na["actor_seat"],
                        "kind": na["kind"],
                        "target_seat": na["target_seat"],
                        "submitted_at_ms": _epoch_to_ms(na["submitted_at"]),
                    }
                    for na in night_actions
                    if na["day"] == day and _night_phase(na["day"]) == phase
                ],
            }
        )
    return result


def _vote_phase(round_: int) -> str:
    # Schema convention: round=0 is the regular vote, round=1 is the runoff.
    # Anything ≥1 is treated as runoff so the exporter is forward-compatible
    # if a future schema adds a second runoff round.
    return "DAY_RUNOFF" if round_ >= 1 else "DAY_VOTE"


def _night_phase(day: int) -> str:
    return "NIGHT_0" if day == 0 else "NIGHT"


def _infer_victory(public_logs: list[dict[str, Any]]) -> str | None:
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
def _load_trace(trace_root: Path, game_id: str) -> list[dict[str, Any]]:
    """Walk ``logs/llm_calls/{game_id}/*.jsonl`` and inline every entry.

    Missing dir = empty list (game ran with trace disabled, or pre-trace
    games). One bad line is logged and skipped — not fatal.
    """
    game_dir = trace_root / game_id
    if not game_dir.is_dir():
        return []
    entries: list[dict[str, Any]] = []
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
                entries.append(obj)
        except OSError:
            log.exception("could not read trace file %s", jsonl_path)
    # Sort by ts when present so the flat list reads chronologically.
    entries.sort(key=lambda e: e.get("ts") or "")
    return entries


__all__ = ["export_game"]
