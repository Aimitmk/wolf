-- Per-utterance / per-vote structured suspicion record. Each row is
-- one "席X が 席Y を level (理由) で見ている" datum, sourced either
-- from a speech (`source='speech'`) or from a vote decision
-- (`source='vote'`).
--
-- For source='speech', `event_id` references `speech_events.event_id`
-- so the suspicion deletes when the parent SpeechEvent's game is
-- removed. For source='vote', `event_id` is NULL because vote
-- decisions don't produce a SpeechEvent — the (vote_day, vote_round,
-- suspecter_seat, target_seat) tuple identifies the vote-derived row.
--
-- Surrogate `id` PK lets event_id be nullable while keeping uniqueness
-- guarantees per (event_id, seq) for speech rows enforceable via the
-- composite UNIQUE index below.
--
-- Anti-fabrication: subsequent prompts surface the full immutable
-- history so a speaker who silently reverses a prior suspicion (e.g.
-- trust → high without setting update_from_level) is detectable.
-- Updates set update_from_level + update_reason to make the shift
-- explicit.
CREATE TABLE IF NOT EXISTS suspicions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'speech',
    event_id TEXT,
    seq INTEGER NOT NULL DEFAULT 0,
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    day INTEGER NOT NULL,
    phase TEXT NOT NULL,
    vote_round INTEGER,
    suspecter_seat INTEGER NOT NULL,
    target_seat INTEGER NOT NULL,
    level TEXT NOT NULL,
    reason TEXT NOT NULL,
    update_from_level TEXT,
    update_reason TEXT,
    created_at_ms INTEGER NOT NULL
);

-- Per-game timeline: walk every suspicion in chronological order so
-- prompt builders and viewer rendering both see the same ordering.
CREATE INDEX IF NOT EXISTS idx_susp_game_day
    ON suspicions(game_id, day, created_at_ms);

-- Per-(suspecter, target) lookup for fabrication detection: given a
-- candidate new suspicion, query the latest prior level so the
-- prompt builder can highlight unannounced reversals.
CREATE INDEX IF NOT EXISTS idx_susp_pair
    ON suspicions(game_id, suspecter_seat, target_seat, created_at_ms);

-- Speech-row uniqueness: one (event_id, seq) pair is unique. Vote rows
-- have event_id=NULL so they bypass this constraint (SQLite treats
-- NULL as distinct in UNIQUE).
CREATE UNIQUE INDEX IF NOT EXISTS idx_susp_event_seq
    ON suspicions(event_id, seq) WHERE event_id IS NOT NULL;

-- Vote-row uniqueness: one suspecter casts at most one vote-derived
-- record per (target, day, round). Re-running the vote dispatch path
-- doesn't duplicate rows.
CREATE UNIQUE INDEX IF NOT EXISTS idx_susp_vote_unique
    ON suspicions(game_id, day, vote_round, suspecter_seat, target_seat)
 WHERE source = 'vote';
