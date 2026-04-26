## ADDED Requirements

### Requirement: SpeechEvent is the unified public-utterance contract

The system SHALL record every public utterance — human voice (post-STT), human Discord text, and NPC-generated text — as a `SpeechEvent` row in the `speech_events` table with the same shape regardless of origin. The schema MUST include `event_id` (ULID), `game_id`, `phase_id`, `day`, `phase`, `source` (`voice_stt | text | npc_generated`), `speaker_kind` (`human | npc`), `speaker_seat`, `text`, `stt_confidence` (nullable), `audio_start_ms` (nullable), `audio_end_ms` (nullable), and `created_at_ms`. Both `LLM_DISCUSSION_MODE` modes MUST write `SpeechEvent` rows for every utterance they produce — `rounds` mode writes one `SpeechEvent(source=npc_generated)` per LLM speech in addition to the existing `PLAYER_SPEECH` log entry.

#### Scenario: Human text utterance is recorded
- **WHEN** a human player sends a Discord main-channel message during `DAY_DISCUSSION`
- **THEN** a `SpeechEvent` is inserted with `source=text`, `speaker_kind=human`, the player's `seat_no`, the message text, and `stt_confidence`/`audio_*` fields all `null`

#### Scenario: NPC utterance under rounds mode is recorded
- **WHEN** the bot is running with `LLM_DISCUSSION_MODE=rounds` and an LLM seat's discussion task posts a `PLAYER_SPEECH` log entry
- **THEN** the same utterance is also recorded as `SpeechEvent(source=npc_generated, speaker_kind=npc)` with the same `seat_no` and `text` and matching `phase_id`

#### Scenario: NPC utterance under reactive_voice mode is recorded
- **WHEN** the bot is running with `LLM_DISCUSSION_MODE=reactive_voice` and an NPC bot's `SpeakResult` is accepted by the Master arbiter
- **THEN** a `SpeechEvent(source=npc_generated, speaker_kind=npc)` is inserted before `PlaybackAuthorized` is dispatched

### Requirement: Human voice produces exactly one finalized SpeechEvent

For human voice input, the system SHALL emit exactly one `SpeechEvent(source=voice_stt)` per utterance, written **after STT completes**. The upstream `vad_speech_started` and `vad_speech_ended` events MUST NOT be persisted as `SpeechEvent` rows; they SHALL only appear as structured-log events on the voice-ingest worker. STT failures and below-threshold confidences MUST NOT produce a `SpeechEvent` (see voice-ingest spec for the failure contract).

#### Scenario: Successful STT writes one SpeechEvent
- **WHEN** voice-ingest receives a complete human VAD segment, runs STT successfully, and the result's `stt_confidence` is at or above the configured threshold
- **THEN** exactly one `SpeechEvent(source=voice_stt, speaker_kind=human)` is inserted with the transcribed `text`, the `stt_confidence`, the `audio_start_ms` / `audio_end_ms` aligned with VAD boundaries, and `created_at_ms = STT completion time`

#### Scenario: VAD events are not stored as SpeechEvent
- **WHEN** voice-ingest detects `vad_speech_started` and later `vad_speech_ended` for a human segment
- **THEN** the `speech_events` table receives no row from those VAD events — only structured-log entries are emitted

### Requirement: PublicDiscussionState is derived deterministically from SpeechEvent history

The Master SHALL maintain an in-memory `PublicDiscussionState` value object per active game that is **derived deterministically** from the `speech_events` rows for the current `phase_id`. The state MUST contain `co_claims`, `stances`, `pressure`, `open_topics`, `silent_seats`, and `recent_speech_event_ids`. The derivation MUST never rely on LLM output — pure code-side computation only. The state MUST be rebuildable from `speech_events` alone (no external state); on Master restart, the state for the current phase SHALL be reconstructed from the persisted rows.

#### Scenario: State rebuilds from log on Master restart
- **WHEN** Master restarts during an active `DAY_DISCUSSION` phase
- **THEN** Master rebuilds `PublicDiscussionState` for the current `phase_id` by re-reading `speech_events` rows whose `phase_id` matches and replaying them through the apply function, producing the same value object that would have been held in memory before restart

#### Scenario: Derivation is pure
- **WHEN** the same sequence of `SpeechEvent` rows is applied twice from a fresh empty state
- **THEN** the two resulting `PublicDiscussionState` values are bitwise equal

### Requirement: MVP derivation rules cover co_claims and silent_seats

For the MVP, `PublicDiscussionState` derivation SHALL implement at minimum two deterministic rules: (1) `co_claims` adds an entry whenever a `SpeechEvent.text` contains a canonical CO token (`占いCO`, `霊媒CO`, `騎士CO`, or `〇〇CO` for any role keyword) attributed to the speaker's seat; (2) `silent_seats` is the set of alive seats with zero `SpeechEvent` rows in the current `phase_id`. The other fields (`stances`, `pressure`, `open_topics`) MAY remain empty in the MVP — their concrete heuristics are decided in the design phase and refined post-MVP.

#### Scenario: Seer CO is detected
- **WHEN** seat 3's `SpeechEvent.text` contains `占いCO` for the first time in `DAY_DISCUSSION`
- **THEN** `PublicDiscussionState.co_claims` for the current phase contains an entry `{seat: 3, role_claim: "seer", ...}`

#### Scenario: Silent seats are tracked
- **WHEN** `DAY_DISCUSSION` is 60 seconds in and seats 6 and 7 have no `SpeechEvent` rows for the current `phase_id`
- **THEN** `PublicDiscussionState.silent_seats` is the unordered set `{6, 7}` (assuming both are alive)

### Requirement: speech_events table is additive and forward-compatible

The `speech_events` table SHALL be created via `CREATE TABLE IF NOT EXISTS` in the existing `migrate()` flow. The DDL MUST be idempotent so that existing databases upgrade cleanly on restart. Adding new columns to the table MUST follow the existing additive pattern (`ALTER TABLE ADD COLUMN` guarded by `PRAGMA table_info`); destructive changes are out of scope.

#### Scenario: Migration runs on every boot
- **WHEN** the bot starts against a database that already has the `speech_events` table
- **THEN** `migrate()` issues no DDL changes for that table because the `IF NOT EXISTS` guard short-circuits

#### Scenario: Migration creates the table on a fresh database
- **WHEN** the bot starts against a database without the `speech_events` table
- **THEN** `migrate()` creates it with the full MVP schema and the bot proceeds to start
