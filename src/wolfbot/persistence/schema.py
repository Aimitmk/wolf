"""SQLite schema + migrate().

All DDL is idempotent via `CREATE TABLE IF NOT EXISTS`. Safe to re-run on every boot.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS games (
        id TEXT PRIMARY KEY,
        guild_id TEXT NOT NULL,
        host_user_id TEXT NOT NULL,
        phase TEXT NOT NULL,
        day_number INTEGER NOT NULL DEFAULT 0,
        deadline_epoch INTEGER,
        main_text_channel_id TEXT NOT NULL,
        main_vc_channel_id TEXT NOT NULL,
        heaven_channel_id TEXT,
        wolves_channel_id TEXT,
        created_at INTEGER NOT NULL,
        ended_at INTEGER,
        force_skip_pending INTEGER NOT NULL DEFAULT 0,
        discussion_mode TEXT NOT NULL DEFAULT 'rounds'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_games_active
        ON games(ended_at) WHERE ended_at IS NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_games_unique_active
        ON games(guild_id) WHERE ended_at IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS seats (
        game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        seat_no INTEGER NOT NULL,
        discord_user_id TEXT,
        display_name TEXT NOT NULL,
        is_llm INTEGER NOT NULL,
        persona_key TEXT,
        role TEXT,
        alive INTEGER NOT NULL DEFAULT 1,
        death_cause TEXT,
        death_day INTEGER,
        dm_channel_id TEXT,
        PRIMARY KEY (game_id, seat_no)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_seats_user ON seats(discord_user_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS night_actions (
        game_id TEXT NOT NULL,
        day INTEGER NOT NULL,
        actor_seat INTEGER NOT NULL,
        kind TEXT NOT NULL,
        target_seat INTEGER,
        submitted_at INTEGER NOT NULL,
        PRIMARY KEY (game_id, day, actor_seat, kind),
        FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS votes (
        game_id TEXT NOT NULL,
        day INTEGER NOT NULL,
        round INTEGER NOT NULL,
        voter_seat INTEGER NOT NULL,
        target_seat INTEGER,
        submitted_at INTEGER NOT NULL,
        PRIMARY KEY (game_id, day, round, voter_seat),
        FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS previous_guard (
        game_id TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
        knight_seat INTEGER NOT NULL,
        last_guard_seat INTEGER,
        last_guard_day INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS logs_public (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id TEXT NOT NULL,
        day INTEGER NOT NULL,
        phase TEXT NOT NULL,
        kind TEXT NOT NULL,
        actor_seat INTEGER,
        text TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pub_logs_game ON logs_public(game_id, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS logs_private (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id TEXT NOT NULL,
        day INTEGER NOT NULL,
        phase TEXT NOT NULL,
        kind TEXT NOT NULL,
        actor_seat INTEGER,
        audience_seat INTEGER,
        text TEXT NOT NULL,
        payload_json TEXT,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_priv_logs_game ON logs_private(game_id, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_decisions (
        game_id TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
        phase TEXT NOT NULL,
        day INTEGER NOT NULL,
        required_submission TEXT NOT NULL,
        missing_seats_json TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_assignments (
        game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        seat_no INTEGER NOT NULL,
        persona_key TEXT NOT NULL,
        PRIMARY KEY (game_id, seat_no),
        UNIQUE (game_id, persona_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_speech_counts (
        game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        day INTEGER NOT NULL,
        seat_no INTEGER NOT NULL,
        normal_count INTEGER NOT NULL DEFAULT 0,
        vote_intent_done INTEGER NOT NULL DEFAULT 0,
        last_spoke_epoch INTEGER,
        discussion_rounds_done INTEGER NOT NULL DEFAULT 0,
        runoff_speech_done INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (game_id, day, seat_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS speech_events (
        event_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        phase_id TEXT NOT NULL,
        day INTEGER NOT NULL,
        phase TEXT NOT NULL,
        source TEXT NOT NULL,
        speaker_kind TEXT NOT NULL,
        speaker_seat INTEGER,
        text TEXT NOT NULL,
        stt_confidence REAL,
        audio_start_ms INTEGER,
        audio_end_ms INTEGER,
        alive_seat_nos_json TEXT,
        summary TEXT,
        created_at_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_speech_events_phase
        ON speech_events(game_id, phase_id, created_at_ms)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_speech_events_seat
        ON speech_events(game_id, phase_id, speaker_seat)
    """,
    """
    CREATE TABLE IF NOT EXISTS npc_speak_requests (
        request_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        phase_id TEXT NOT NULL,
        npc_id TEXT NOT NULL,
        seat_no INTEGER NOT NULL,
        logic_packet_id TEXT NOT NULL,
        suggested_intent TEXT NOT NULL,
        max_chars INTEGER NOT NULL,
        max_duration_ms INTEGER NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        expires_at_ms INTEGER NOT NULL,
        created_at_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_npc_speak_requests_game_phase
        ON npc_speak_requests(game_id, phase_id, expires_at_ms)
    """,
    """
    CREATE TABLE IF NOT EXISTS npc_speak_results (
        request_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        phase_id TEXT NOT NULL,
        npc_id TEXT NOT NULL,
        status TEXT NOT NULL,
        text TEXT,
        used_logic_ids_json TEXT,
        intent TEXT,
        estimated_duration_ms INTEGER,
        failure_reason TEXT,
        received_at_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_npc_speak_results_phase
        ON npc_speak_results(game_id, phase_id, received_at_ms)
    """,
    """
    CREATE TABLE IF NOT EXISTS npc_playback_events (
        request_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        phase_id TEXT NOT NULL,
        npc_id TEXT NOT NULL,
        speech_event_id TEXT,
        authorized_at_ms INTEGER NOT NULL,
        playback_deadline_ms INTEGER NOT NULL,
        finished_at_ms INTEGER,
        outcome TEXT,
        failure_reason TEXT,
        tts_outcome TEXT,
        tts_duration_ms INTEGER,
        tts_failure_reason TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_npc_playback_events_open
        ON npc_playback_events(game_id, finished_at_ms)
        WHERE finished_at_ms IS NULL
    """,
]


async def migrate(db_path: str | Path) -> None:
    """Create tables if they don't exist. Safe to call on every boot."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(path)) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode = WAL")
        for stmt in DDL:
            await db.execute(stmt)
        # Additive column migrations: SQLite doesn't support
        # `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so we guard with PRAGMA.
        # Each new column added to a CREATE TABLE above must also be guarded
        # here, otherwise old DBs upgraded in place keep the pre-add schema
        # and subsequent INSERTs fail with "no column named ...".
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
        await db.commit()
