-- Per-night submissions from role-holders: wolf attack / seer divine /
-- knight guard. Composite PK enforces "one submission per actor per kind
-- per day" — re-submissions overwrite via INSERT OR REPLACE in service code.
CREATE TABLE IF NOT EXISTS night_actions (
    game_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    actor_seat INTEGER NOT NULL,
    kind TEXT NOT NULL,
    target_seat INTEGER,
    submitted_at INTEGER NOT NULL,
    PRIMARY KEY (game_id, day, actor_seat, kind),
    FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
);
