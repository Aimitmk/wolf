-- Day votes (round=0) and runoff votes (round=1+). Composite PK enforces
-- "one ballot per voter per round per day"; null target_seat encodes
-- a legitimate abstain.
CREATE TABLE IF NOT EXISTS votes (
    game_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    round INTEGER NOT NULL,
    voter_seat INTEGER NOT NULL,
    target_seat INTEGER,
    submitted_at INTEGER NOT NULL,
    PRIMARY KEY (game_id, day, round, voter_seat),
    FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
);
