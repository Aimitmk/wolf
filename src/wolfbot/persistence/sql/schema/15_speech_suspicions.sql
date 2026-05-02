-- Per-utterance structured suspicion record. Each row is one
-- "席X が 席Y に level=high (理由)" datum attached to a SpeechEvent.
-- Composite primary key (event_id, seq) lets a single utterance carry
-- multiple suspicions while keeping the row immutable once written.
--
-- Anti-fabrication: subsequent prompts surface the full immutable
-- history so a speaker who silently reverses a prior suspicion
-- (e.g. trust → high without setting update_from_level) is detectable.
-- Updates set update_from_level + update_reason to make the shift
-- explicit.
CREATE TABLE IF NOT EXISTS speech_suspicions (
    event_id TEXT NOT NULL REFERENCES speech_events(event_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    day INTEGER NOT NULL,
    phase TEXT NOT NULL,
    suspecter_seat INTEGER NOT NULL,
    target_seat INTEGER NOT NULL,
    level TEXT NOT NULL,
    reason TEXT NOT NULL,
    update_from_level TEXT,
    update_reason TEXT,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (event_id, seq)
);

-- Per-game timeline: walk every suspicion in chronological order so
-- prompt builders and viewer rendering both see the same ordering.
CREATE INDEX IF NOT EXISTS idx_susp_game_day
    ON speech_suspicions(game_id, day, created_at_ms);

-- Per-(suspecter, target) lookup for fabrication detection: given a
-- candidate new suspicion, query the latest prior level so the
-- prompt builder can highlight unannounced reversals.
CREATE INDEX IF NOT EXISTS idx_susp_pair
    ON speech_suspicions(game_id, suspecter_seat, target_seat, created_at_ms);
