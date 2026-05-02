-- Per-LLM-seat speech progress per day so /wolf force-skip / restart
-- mid-flight resumes without double-posting. discussion_rounds_done
-- and runoff_speech_done flip from 0 to 1 once the corresponding
-- speech batch settles for that seat.
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
);
