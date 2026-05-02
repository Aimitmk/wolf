"""SQLite schema + migrate().

DDL for the base ``CREATE TABLE`` / ``CREATE INDEX`` statements lives in
:mod:`wolfbot.persistence.sql` as one ``NN_<entity>.sql`` file per
logical table group; :func:`migrate` loads them in lex order and
executes each statement. All CREATE statements are idempotent via
``IF NOT EXISTS`` so the routine is safe to re-run on every boot.

Additive column migrations stay in Python because SQLite has no
``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``. The block at the bottom
of :func:`migrate` walks ``PRAGMA table_info(<table>)`` and only adds
the column when missing, which keeps existing DBs upgrading cleanly
without dropping data.

When you add a new column to one of the ``schema/*.sql`` files, you
MUST also add a guarded ``ALTER TABLE`` here, otherwise old DBs
upgraded in place keep the pre-add schema and subsequent INSERTs fail
with "no column named ...".
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from wolfbot.persistence.sql import load_schema_script


async def migrate(db_path: str | Path) -> None:
    """Create tables if they don't exist. Safe to call on every boot."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(path)) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode = WAL")
        # `executescript` runs the concatenated DDL through SQLite's own
        # parser, which strips `--` line comments and `/* */` block
        # comments before splitting on `;`. A Python-side `split(';')`
        # tripped on the first `--` comment whose body contained `;`.
        await db.executescript(load_schema_script())
        # Additive column migrations — see module docstring for the
        # reason these live in Python.
        async with db.execute("PRAGMA table_info(games)") as cur:
            cols = {row[1] async for row in cur}
        if "force_skip_pending" not in cols:
            await db.execute(
                "ALTER TABLE games ADD COLUMN force_skip_pending INTEGER NOT NULL DEFAULT 0"
            )
        if "discussion_mode" not in cols:
            await db.execute(
                "ALTER TABLE games ADD COLUMN discussion_mode TEXT NOT NULL DEFAULT 'rounds'"
            )
        async with db.execute("PRAGMA table_info(seats)") as cur:
            cols = {row[1] async for row in cur}
        if "dm_channel_id" not in cols:
            await db.execute("ALTER TABLE seats ADD COLUMN dm_channel_id TEXT")
        async with db.execute("PRAGMA table_info(pending_decisions)") as cur:
            cols = {row[1] async for row in cur}
        if "submissions_json" not in cols:
            await db.execute("ALTER TABLE pending_decisions ADD COLUMN submissions_json TEXT")
        async with db.execute("PRAGMA table_info(llm_speech_counts)") as cur:
            cols = {row[1] async for row in cur}
        if "discussion_rounds_done" not in cols:
            await db.execute(
                "ALTER TABLE llm_speech_counts "
                "ADD COLUMN discussion_rounds_done INTEGER NOT NULL DEFAULT 0"
            )
        if "runoff_speech_done" not in cols:
            await db.execute(
                "ALTER TABLE llm_speech_counts "
                "ADD COLUMN runoff_speech_done INTEGER NOT NULL DEFAULT 0"
            )
        async with db.execute("PRAGMA table_info(speech_events)") as cur:
            cols = {row[1] async for row in cur}
        if "summary" not in cols:
            await db.execute("ALTER TABLE speech_events ADD COLUMN summary TEXT")
        if "co_declaration" not in cols:
            await db.execute(
                "ALTER TABLE speech_events ADD COLUMN co_declaration TEXT"
            )
        if "addressed_seat_no" not in cols:
            await db.execute(
                "ALTER TABLE speech_events ADD COLUMN addressed_seat_no INTEGER"
            )
        if "addressed_seat_nos_json" not in cols:
            await db.execute(
                "ALTER TABLE speech_events ADD COLUMN addressed_seat_nos_json TEXT"
            )
        if "role_callout" not in cols:
            await db.execute(
                "ALTER TABLE speech_events ADD COLUMN role_callout TEXT"
            )
        if "claimed_seer_target_seat" not in cols:
            await db.execute(
                "ALTER TABLE speech_events "
                "ADD COLUMN claimed_seer_target_seat INTEGER"
            )
        if "claimed_seer_is_wolf" not in cols:
            await db.execute(
                "ALTER TABLE speech_events "
                "ADD COLUMN claimed_seer_is_wolf INTEGER"
            )
        if "claimed_medium_target_seat" not in cols:
            await db.execute(
                "ALTER TABLE speech_events "
                "ADD COLUMN claimed_medium_target_seat INTEGER"
            )
        if "claimed_medium_is_wolf" not in cols:
            await db.execute(
                "ALTER TABLE speech_events "
                "ADD COLUMN claimed_medium_is_wolf INTEGER"
            )
        async with db.execute("PRAGMA table_info(npc_speak_requests)") as cur:
            cols = {row[1] async for row in cur}
        if "selection_reason" not in cols:
            await db.execute(
                "ALTER TABLE npc_speak_requests ADD COLUMN selection_reason TEXT"
            )
        if "public_state_snapshot_json" not in cols:
            await db.execute(
                "ALTER TABLE npc_speak_requests "
                "ADD COLUMN public_state_snapshot_json TEXT"
            )
        await db.commit()


__all__ = ["migrate"]
