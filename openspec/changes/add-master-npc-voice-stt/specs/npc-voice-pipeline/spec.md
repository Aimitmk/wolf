## ADDED Requirements

### Requirement: Each NPC runs as its own Discord bot process

Each NPC seat in `LLM_DISCUSSION_MODE=reactive_voice` SHALL be served by a separate `wolfbot-npc` worker process with its own Discord bot user, its own `NPC_DISCORD_TOKEN`, its own `NPC_ID`, its own `TTS_VOICE_ID`, and its own WebSocket connection to Master. The Master process MUST NOT play TTS audio itself or impersonate any NPC's Discord identity.

#### Scenario: NPC bot uses a distinct Discord identity
- **WHEN** an NPC bot connects to Discord with token `NPC_DISCORD_TOKEN`
- **THEN** the resulting `discord_bot_user_id` is distinct from the Master bot's user id and from every other NPC bot's user id

#### Scenario: Master never plays NPC audio
- **WHEN** Master is asked to coordinate NPC speech in any phase
- **THEN** Master never directly invokes a TTS provider or plays audio in the voice channel; only the NPC bot for the speaking seat does

### Requirement: Master <-> NPC transport is localhost WebSocket with pre-shared key

The MVP Master ↔ NPC transport SHALL be a WebSocket bound to `127.0.0.1` on Master and connected from each NPC bot worker on the same host. Authentication MUST use a single pre-shared key carried in `MASTER_NPC_PSK` (read by Master and by every NPC bot). The protocol MUST refuse any connection that does not present the correct key. Multi-host deployment, TLS, and per-NPC rotating tokens are out of scope for the MVP.

#### Scenario: Master rejects a connection with a wrong PSK
- **WHEN** an NPC bot attempts to connect with an incorrect `MASTER_NPC_PSK`
- **THEN** Master closes the connection immediately and emits a structured log event with `event=npc_register_rejected` and `failure_reason=invalid_psk`

#### Scenario: Master accepts a localhost connection with the correct PSK
- **WHEN** an NPC bot connects from `127.0.0.1` with the correct `MASTER_NPC_PSK`
- **THEN** Master accepts the connection, runs the registration handshake below, and replies with `npc_registered`

### Requirement: NPC registration produces a typed handshake

On connect, an NPC bot SHALL send a typed `npc_register` message containing `npc_id`, `discord_bot_user_id`, `supported_voices` (a list of `voice_id`s), and `version`. Master SHALL respond with `npc_registered` containing `assigned_seat`, `game_id`, and `phase_id`, after which the NPC is considered live in the registry. NPC bots MUST send `heartbeat` messages at a configurable interval; Master MUST treat an NPC as offline if no heartbeat arrives within a configurable timeout.

#### Scenario: NPC registers and is bound to a seat
- **WHEN** an NPC bot sends `npc_register` for `npc_id=npc_p5` while seat 5 of game `g_123` is in setup or active
- **THEN** Master maps `npc_p5` to seat 5 of `g_123` in its in-memory NPC registry and responds with `npc_registered` carrying `assigned_seat=5`

#### Scenario: Heartbeat timeout marks an NPC offline
- **WHEN** Master receives no heartbeat from an NPC for longer than the configured timeout
- **THEN** the NPC is marked offline in the registry and any subsequent `SpeakArbiter` evaluation skips it for the affected phase

### Requirement: Master-side speak arbitration drives the reactive_voice flow

When `LLM_DISCUSSION_MODE=reactive_voice`, Master SHALL implement `MasterLogicBuilder` and `SpeakArbiter`. `MasterLogicBuilder` reads `PublicDiscussionState` and produces per-NPC `LogicPacket` payloads with `packet_id`, `phase_id`, `recipient_npc_id`, a textual `public_state_summary`, a list of `logic_candidates` (each with `id`, `claim`, `support[]`, `counter[]`), per-seat `pressure` weights, and `expires_at_ms`. `SpeakArbiter` MUST decide which NPC should speak next, dispatch a typed `SpeakRequest` over the WebSocket carrying `request_id`, `phase_id`, `npc_id`, `seat_no`, `logic_packet_id`, `suggested_intent`, `max_chars=80`, `max_duration_ms`, `priority`, and `expires_at_ms`, and persist a row in `npc_speak_requests`.

#### Scenario: SpeakRequest is sent only to alive NPCs
- **WHEN** `SpeakArbiter` selects a candidate NPC who is alive and online
- **THEN** Master sends a `SpeakRequest` over the WebSocket and inserts a `npc_speak_requests` row with the same `request_id` and `phase_id`

#### Scenario: Dead or offline NPC is not sent SpeakRequest
- **WHEN** `SpeakArbiter` evaluates a candidate NPC seat that is dead or whose WebSocket is offline
- **THEN** no `SpeakRequest` is dispatched and no `npc_speak_requests` row is created for that turn

### Requirement: Speech is strictly serial inside a game

While a human speaker has an open utterance (after `vad_speech_started` and before either `vad_speech_ended` AND a finalized `SpeechEvent(source=voice_stt)` arrives, or a configurable timeout elapses) OR another NPC's `PlaybackAuthorized` window is still open, `SpeakArbiter` SHALL NOT issue a new `SpeakRequest`. If a `SpeakRequest` would be appropriate but blocked, the arbiter MUST emit a structured log event `speak_request_suppressed` with `failure_reason=human_currently_speaking` or `failure_reason=queue_busy`.

#### Scenario: NPC speech blocked while human is speaking
- **WHEN** voice-ingest reports `vad_speech_started` for seat 3 and `vad_speech_ended` has not arrived
- **THEN** `SpeakArbiter` emits no `SpeakRequest` for any NPC and logs `speak_request_suppressed` with `failure_reason=human_currently_speaking` if a candidate was otherwise eligible

#### Scenario: NPC speech blocked while another NPC plays back
- **WHEN** an NPC's `PlaybackAuthorized` window is open and the arbiter evaluates a different NPC candidate
- **THEN** no `SpeakRequest` is sent to the second NPC; `speak_request_suppressed` with `failure_reason=queue_busy` is logged

### Requirement: NPC bot generates a short utterance via Grok and returns SpeakResult

On receipt of a `SpeakRequest`, the NPC bot SHALL build a Grok prompt by combining its own persona / role / private state with the `LogicPacket` referenced by `logic_packet_id`, call the existing xAI structured-output endpoint, and return a typed `SpeakResult` to Master containing `request_id`, `npc_id`, `phase_id`, `status` (`accepted | declined | error`), `text` (≤80 characters when `status=accepted`), `used_logic_ids`, `intent`, and `estimated_duration_ms`. NPC bots MUST NOT play audio before sending `SpeakResult`.

#### Scenario: NPC produces an accepted SpeakResult under 80 characters
- **WHEN** an NPC bot receives a `SpeakRequest` and Grok returns a valid utterance
- **THEN** the NPC bot sends `SpeakResult` with `status=accepted`, `text` not exceeding 80 characters, `used_logic_ids` referencing one or more entries from the matching `LogicPacket`, and an `estimated_duration_ms`

#### Scenario: NPC bot does not pre-play audio
- **WHEN** an NPC bot has a finished Grok response but has not yet received `PlaybackAuthorized`
- **THEN** no audio is queued or played; the NPC bot only emits `SpeakResult` and waits

### Requirement: Master validates SpeakResult against current phase

When `SpeakResult` arrives, Master SHALL validate `phase_id` against the current `phase_id` of the game and `request_id` against an open `npc_speak_requests` row whose `expires_at_ms` has not yet passed. On validation success, Master inserts a row in `npc_speak_results` with `status=accepted`, writes a `SpeechEvent(source=npc_generated, speaker_kind=npc)` for the seat, and dispatches `PlaybackAuthorized` over the WebSocket. On validation failure (stale phase, expired request, unknown request_id), Master writes the result row with `status=rejected` and an explicit `failure_reason` (`stale_phase`, `expired_request`, or `unknown_request`), emits a structured log, and returns a `playback_rejected` message to the NPC bot.

#### Scenario: Stale phase result is rejected
- **WHEN** an NPC's `SpeakResult` arrives carrying `phase_id=day1_discussion_001` after Master has already advanced to `day1_vote_001`
- **THEN** the result is recorded with `status=rejected`, `failure_reason=stale_phase`; no `SpeechEvent` is written; the NPC bot receives `playback_rejected`

#### Scenario: Accepted SpeakResult yields PlaybackAuthorized
- **WHEN** Master accepts a `SpeakResult` whose `phase_id` matches and whose `request_id` is open and unexpired
- **THEN** Master writes the corresponding `npc_speak_results` row with `status=accepted`, inserts the matching `SpeechEvent(source=npc_generated)`, and sends `PlaybackAuthorized` to the NPC bot with `speech_event_id` and `playback_deadline_ms`

### Requirement: NPC bot plays TTS only after PlaybackAuthorized

NPC bots MUST gate VC playback on receiving a `PlaybackAuthorized` for a specific `request_id`. On receipt, the NPC bot calls its TTS provider with the accepted `text` and the configured `TTS_VOICE_ID`, then plays the resulting audio in `MAIN_VOICE_CHANNEL_ID`. Once playback finishes, the NPC bot SHALL send a typed `playback_finished` message to Master. NPC bots MUST NOT play any audio for which they did not receive a matching `PlaybackAuthorized`.

#### Scenario: Playback occurs only post-authorization
- **WHEN** an NPC bot receives `PlaybackAuthorized` for `request_id=sr_01HX`
- **THEN** the NPC bot runs TTS for the corresponding `text`, plays the audio in the voice channel, and emits `playback_finished` after the audio completes

#### Scenario: Without authorization, no playback occurs
- **WHEN** an NPC bot has sent `SpeakResult` and either receives `playback_rejected` or no `PlaybackAuthorized` arrives within the configured timeout
- **THEN** the NPC bot performs no TTS call and plays no audio for that `request_id`

### Requirement: PlaybackAuthorized window closes on playback_finished or deadline

A `PlaybackAuthorized` window SHALL be closed on Master when **either** the matching `playback_finished` message arrives over the WebSocket OR Master's clock passes `playback_deadline_ms` for that request — whichever comes first. Deadline-driven closures MUST be logged with `failure_reason=tts_timeout` (no playback_finished received and no playback_failed reported) or `failure_reason=discord_playback_error` (playback_failed reported but window already deadline-closed). Once the window closes, `SpeakArbiter` is unblocked from issuing the next `SpeakRequest`.

#### Scenario: Window closes on playback_finished
- **WHEN** Master receives a `playback_finished` for an open authorization window
- **THEN** Master records a row in `npc_playback_events` with `status=succeeded` and `finished_at` set, and the serial-speech gate is released

#### Scenario: Window closes on deadline timeout
- **WHEN** Master's clock passes `playback_deadline_ms` for an open authorization window without a `playback_finished` arriving
- **THEN** Master writes `npc_playback_events` with `status=failed` and `failure_reason=tts_timeout`, releases the serial-speech gate, and emits a structured log

### Requirement: NPC disconnection causes that NPC to silently sit out the round

If `SpeakArbiter` evaluates a candidate NPC for whom no recent heartbeat exists or whose WebSocket is offline, that NPC SHALL silently stay out of the round; the game continues, votes and night actions still resolve through the existing DM flow. There MUST be no automatic fallback to text-mode speech for that NPC during the affected phase. The skip MUST be logged as `speak_candidate_skipped` with `failure_reason=npc_offline`.

#### Scenario: Offline NPC is skipped
- **WHEN** `SpeakArbiter` evaluates seat 5 and seat 5's NPC bot has not heartbeat in N seconds
- **THEN** no `SpeakRequest` is sent to seat 5 and a structured log event `speak_candidate_skipped` with `failure_reason=npc_offline` is recorded

### Requirement: Restart drops in-flight requests and authorizations

If Master, voice-ingest, or any NPC bot restarts during `DAY_DISCUSSION`, in-flight protocol state at that process boundary SHALL be dropped. On Master restart, every open `npc_speak_requests` row whose `npc_speak_results` is missing MUST be marked rejected with `failure_reason=master_restart`, and every open `npc_playback_events` row without a paired `playback_finished` MUST be marked failed with `failure_reason=master_restart`. NPC bot restart and voice-ingest restart MUST emit structured-log events (`npc_restart`, `voice_ingest_restart`). On Master restart, `PublicDiscussionState` is rebuilt from `speech_events` for the active phase. The existing `RecoveryService` rule (`deadline_epoch < now` parks in `WAITING_HOST_DECISION`) MUST be preserved.

#### Scenario: Master restart marks open requests rejected
- **WHEN** Master restarts mid-phase with two open `SpeakRequest`s outstanding
- **THEN** the recovery flow inserts `npc_speak_results` rows with `status=rejected`, `failure_reason=master_restart` for both, and Master rebuilds `PublicDiscussionState` from `speech_events` before resuming `SpeakArbiter`

### Requirement: Required tables exist with idempotent migrations

The following tables SHALL be created via additive `CREATE TABLE IF NOT EXISTS` migrations: `npc_speak_requests`, `npc_speak_results`, `npc_playback_events`. Each MUST carry the fields necessary to reconstruct a per-request audit trail (request id, phase id, npc id, seat, status, failure_reason, timestamps).

#### Scenario: Migrations are idempotent on existing databases
- **WHEN** the bot starts against a database that already has these tables
- **THEN** `migrate()` issues no DDL changes for them

#### Scenario: Audit trail is queryable by request_id
- **WHEN** a single `request_id` is queried across `npc_speak_requests`, `npc_speak_results`, and `npc_playback_events`
- **THEN** the join produces a complete lifecycle record (sent → result accepted/rejected → playback succeeded/failed/timeout) for that request

### Requirement: All external surfaces are testable via Protocol Fakes

NPC bots, the Master ↔ NPC transport, the TTS provider, the Discord VC playback, and the per-NPC Grok client SHALL each be reached through a Protocol so tests substitute Fakes. New testing utilities MUST include `FakeNpcClient`, `FakeMasterWsServer`, `FakeTtsService`, and `FakeVoicePlayback`. No test in the suite may make real Discord, xAI, Gemini, or TTS-provider calls.

#### Scenario: Reactive-voice flow is tested with Fakes only
- **WHEN** a unit test exercises the full SpeakArbiter → SpeakRequest → SpeakResult → PlaybackAuthorized → playback_finished cycle
- **THEN** it can do so by composing the Master service, an in-memory `FakeMasterWsServer`, a `FakeNpcClient`, a `FakeTtsService`, and a `FakeVoicePlayback`, without touching any real network
