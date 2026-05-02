-- Reactive_voice's append-only feed of every public speech event:
-- text input, voice STT, NPC-generated, plus the phase_baseline
-- sentinel. Structured analyzer fields (summary / co_declaration /
-- addressed_seat_no(_s) / role_callout / claimed_seer/medium_*)
-- carry the per-event JSON the analyzer LLM extracted, so the
-- claim_history fold doesn't need to re-parse the raw text.
CREATE TABLE IF NOT EXISTS speech_events (
    event_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    phase_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    phase TEXT NOT NULL,
    source TEXT NOT NULL,
    speaker_kind TEXT NOT NULL,
    speaker_seat INTEGER,
    text TEXT NOT NULL,
    stt_confidence REAL,
    audio_start_ms INTEGER,
    audio_end_ms INTEGER,
    alive_seat_nos_json TEXT,
    summary TEXT,
    co_declaration TEXT,
    addressed_seat_no INTEGER,
    addressed_seat_nos_json TEXT,
    role_callout TEXT,
    claimed_seer_target_seat INTEGER,
    claimed_seer_is_wolf INTEGER,
    claimed_medium_target_seat INTEGER,
    claimed_medium_is_wolf INTEGER,
    created_at_ms INTEGER NOT NULL
);

-- Phase rebuild: walk every event in a phase in chronological order.
CREATE INDEX IF NOT EXISTS idx_speech_events_phase
    ON speech_events(game_id, phase_id, created_at_ms);

-- Per-seat slice within a phase: speech_count rotation, last-speaker
-- LRU, claim history per claimer.
CREATE INDEX IF NOT EXISTS idx_speech_events_seat
    ON speech_events(game_id, phase_id, speaker_seat);
