-- Knight's previous-night guard target — used by `legal_guard_targets`
-- to enforce "no consecutive guard on the same target".
-- Singleton-per-game (PK on game_id) since 9-player has only one knight.
CREATE TABLE IF NOT EXISTS previous_guard (
    game_id TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    knight_seat INTEGER NOT NULL,
    last_guard_seat INTEGER,
    last_guard_day INTEGER
);
