-- Locks each LLM seat to one persona for the duration of a game.
-- UNIQUE on (game_id, persona_key) prevents two seats from sharing a
-- persona; PK on (game_id, seat_no) prevents a seat from holding two.
CREATE TABLE IF NOT EXISTS persona_assignments (
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    seat_no INTEGER NOT NULL,
    persona_key TEXT NOT NULL,
    PRIMARY KEY (game_id, seat_no),
    UNIQUE (game_id, persona_key)
);
