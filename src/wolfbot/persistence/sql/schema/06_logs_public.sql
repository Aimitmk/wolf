-- Public-channel-visible log entries — phase changes, vote results,
-- executions, victories, role reveals. AUTOINCREMENT so created_at
-- ties don't collapse rows during recovery sweeps.
CREATE TABLE IF NOT EXISTS logs_public (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    phase TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor_seat INTEGER,
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pub_logs_game ON logs_public(game_id, created_at);
