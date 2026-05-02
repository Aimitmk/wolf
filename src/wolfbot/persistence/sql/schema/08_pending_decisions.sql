-- Singleton-per-game record of a paused phase that needs the host to
-- /wolf force-skip or /wolf extend. submissions_json is added by the
-- migration block and carries the per-kind PendingSubmission breakdown.
CREATE TABLE IF NOT EXISTS pending_decisions (
    game_id TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    day INTEGER NOT NULL,
    required_submission TEXT NOT NULL,
    missing_seats_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
