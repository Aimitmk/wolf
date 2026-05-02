-- Games table — one row per Werewolf session, lifecycle-tracked by ended_at.
-- All FK-bearing tables below cascade on this row's id.
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
);

-- Recovery sweep: list every active game (= ended_at IS NULL) cheaply on boot.
CREATE INDEX IF NOT EXISTS idx_games_active
    ON games(ended_at) WHERE ended_at IS NULL;

-- Per-guild active singleton: at most one in-flight game per guild at a time.
-- The partial unique index is the structural enforcement; service code also
-- checks this at /wolf start, but the DB is the source of truth.
CREATE UNIQUE INDEX IF NOT EXISTS idx_games_unique_active
    ON games(guild_id) WHERE ended_at IS NULL;
