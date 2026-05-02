-- One row per SpeakResult received from an NPC bot (or synthesized by
-- the arbiter on rejection). status carries accepted / rejected /
-- timed_out; failure_reason gives the canonical machine reason.
CREATE TABLE IF NOT EXISTS npc_speak_results (
    request_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    phase_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    status TEXT NOT NULL,
    text TEXT,
    used_logic_ids_json TEXT,
    intent TEXT,
    estimated_duration_ms INTEGER,
    failure_reason TEXT,
    received_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_npc_speak_results_phase
    ON npc_speak_results(game_id, phase_id, received_at_ms);
