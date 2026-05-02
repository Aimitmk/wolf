-- Per-seat private log entries — role notices, seer/medium/guard results,
-- wolf-partner reveals. Audience_seat is the recipient; payload_json
-- carries structured details for the export pipeline.
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
);

CREATE INDEX IF NOT EXISTS idx_priv_logs_game ON logs_private(game_id, created_at);
