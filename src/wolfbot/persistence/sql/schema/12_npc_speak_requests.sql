-- One row per SpeakRequest dispatched by the arbiter to an NPC bot.
-- public_state_snapshot_json captures the picker's view of state at
-- dispatch time so the viewer can replay why this seat won.
CREATE TABLE IF NOT EXISTS npc_speak_requests (
    request_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    phase_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    seat_no INTEGER NOT NULL,
    logic_packet_id TEXT NOT NULL,
    suggested_intent TEXT NOT NULL,
    max_chars INTEGER NOT NULL,
    max_duration_ms INTEGER NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    expires_at_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    selection_reason TEXT,
    public_state_snapshot_json TEXT
);

-- TTL sweep: list dispatches whose expires_at_ms has elapsed without
-- a SpeakResult, scoped to the phase to keep the index small.
CREATE INDEX IF NOT EXISTS idx_npc_speak_requests_game_phase
    ON npc_speak_requests(game_id, phase_id, expires_at_ms);
