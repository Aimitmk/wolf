-- One row per NPC playback window — opened on SpeakResult acceptance
-- with an authorized_at_ms / playback_deadline_ms pair, closed when
-- tts_finished + playback_finished arrive (or the deadline expires).
-- The partial index keeps "still-open windows" cheap to scan during
-- the periodic deadline sweep.
CREATE TABLE IF NOT EXISTS npc_playback_events (
    request_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    phase_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    speech_event_id TEXT,
    authorized_at_ms INTEGER NOT NULL,
    playback_deadline_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    outcome TEXT,
    failure_reason TEXT,
    tts_outcome TEXT,
    tts_duration_ms INTEGER,
    tts_failure_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_npc_playback_events_open
    ON npc_playback_events(game_id, finished_at_ms)
    WHERE finished_at_ms IS NULL;
