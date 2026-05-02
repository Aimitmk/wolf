-- Seats — fixed 9 per game, role assigned in NIGHT_0 plan.
-- discord_user_id is NULL for LLM seats (persona_key carries the
-- character identity instead).
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
);

-- "Which game is user X currently in?" lookup — used when a DM submission
-- needs to find the active game for the player without scanning all games.
CREATE INDEX IF NOT EXISTS idx_seats_user ON seats(discord_user_id);
