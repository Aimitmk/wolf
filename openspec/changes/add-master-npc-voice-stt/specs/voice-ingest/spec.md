## ADDED Requirements

### Requirement: voice-ingest runs as a separate worker process

A dedicated `voice-ingest` worker SHALL be implemented as a separate process from Master and from each NPC bot. The worker connects to the same Discord guild as Master, joins the voice channel identified by `MAIN_VOICE_CHANNEL_ID` (shared with Master and all NPC bots), captures incoming voice packets, runs VAD per speaker, performs STT on completed segments, and sends `SpeechEvent(source=voice_stt)` records to Master over a Master-defined ingestion endpoint (HTTP POST or WebSocket — chosen in design). The worker MUST NOT inline VC playback, NPC speech generation, or game-state mutation.

#### Scenario: voice-ingest joins MAIN_VOICE_CHANNEL_ID
- **WHEN** the `voice-ingest` worker starts and Master has an active game
- **THEN** the worker joins the Discord voice channel whose id equals `MAIN_VOICE_CHANNEL_ID` and registers as a listener-only client (no audio playback)

#### Scenario: voice-ingest does not write SpeechEvent directly to SQLite
- **WHEN** voice-ingest produces a transcribed utterance
- **THEN** it sends the `SpeechEvent` payload to Master's ingestion endpoint; Master is the sole writer of `speech_events` rows

### Requirement: NPC bot audio is excluded at the receive boundary

voice-ingest SHALL exclude NPC TTS audio from STT processing **before** VAD runs. The worker MUST read the Master NPC registry (over the same protocol it uses to send `SpeechEvent`s) and build the set of `discord_bot_user_id` values currently registered as NPCs. Any inbound voice packet whose Discord `user_id` is in that set MUST be discarded immediately at the receive boundary; such packets MUST NOT be stored, mixed, or fed into VAD/STT.

#### Scenario: NPC voice packet is dropped
- **WHEN** voice-ingest receives a voice packet whose `user_id` matches a registered NPC bot's `discord_bot_user_id`
- **THEN** the packet is discarded immediately and no `vad_speech_started` event is emitted for that packet

#### Scenario: Human voice packet proceeds to VAD
- **WHEN** voice-ingest receives a voice packet whose `user_id` is not in the NPC registry set
- **THEN** the packet is forwarded into the per-speaker VAD pipeline

### Requirement: VAD lifecycle is logged but not persisted as SpeechEvent

voice-ingest SHALL emit structured log events `vad_speech_started` and `vad_speech_ended` for each detected human voice segment, including `game_id`, `phase_id`, `speaker_user_id`, `speaker_seat`, `audio_start_ms` / `audio_end_ms`, and `trace_id`. These events MUST NOT cause `SpeechEvent` rows to be written; only the post-STT finalized utterance does (see speech-event-bus spec).

#### Scenario: VAD start emits a log event
- **WHEN** the VAD engine detects voice activity beginning for a human speaker
- **THEN** voice-ingest emits a structured log entry with `event=vad_speech_started`, the speaker's `user_id`, the resolved `seat_no`, and the absolute `audio_start_ms`

### Requirement: STT uses Gemini API audio input for the MVP

The voice-ingest worker SHALL use the Gemini API audio-input feature as the MVP STT provider. Provider configuration (API key, model id, language hint, confidence threshold) MUST come from environment variables; no provider credentials may be hard-coded. The STT call MUST run asynchronously from the VAD pipeline so a slow STT response does not stall packet capture.

#### Scenario: Gemini API is invoked with the segment audio
- **WHEN** a complete human VAD segment becomes available
- **THEN** voice-ingest sends the segment audio to the Gemini API audio endpoint and awaits the transcription with a configurable timeout

### Requirement: Low-confidence and failed STT are dropped with structured-log evidence

When STT returns a transcription whose `stt_confidence` is below the configured threshold, or when the STT provider returns a hard error (timeout, 5xx, malformed response), voice-ingest SHALL **drop the utterance**: no `SpeechEvent` is sent to Master, and the human player receives no Discord-side surfacing. The drop MUST be recorded as a structured-log event with `event=stt_request_failed` (provider error) or `event=stt_low_confidence` (below threshold), including `failure_reason`, `stt_confidence` (if known), `audio_duration_ms`, and `trace_id`.

#### Scenario: Below-threshold STT result is dropped
- **WHEN** STT returns `stt_confidence = 0.42` and the configured threshold is `0.6`
- **THEN** voice-ingest emits a `stt_low_confidence` log event and does NOT send a `SpeechEvent` to Master

#### Scenario: Provider error drops the utterance
- **WHEN** the Gemini API call raises a timeout or returns a 5xx status
- **THEN** voice-ingest emits a `stt_request_failed` log event with `failure_reason=stt_provider_error` (or `stt_timeout`) and does NOT send a `SpeechEvent` to Master

### Requirement: voice-ingest is fully testable via Protocol Fakes

The voice-ingest worker SHALL expose its external dependencies (Discord VC audio source, STT provider, Master ingestion endpoint, NPC-registry lookup) through Protocols so that tests can substitute Fakes. New testing utilities `FakeSttService`, `FakeNpcRegistryClient`, and `FakeMasterIngestionClient` MUST be provided in `tests/fakes.py` (or a new `tests/voice_fakes.py`). No test in the suite may make real Discord, Gemini, or Master HTTP calls.

#### Scenario: STT can be substituted in tests
- **WHEN** a unit test instantiates the voice-ingest pipeline
- **THEN** it can pass a `FakeSttService` that returns predetermined transcriptions and confidences without making any external network call

### Requirement: voice-ingest restart drops in-flight VAD windows

When the voice-ingest worker restarts during an active `DAY_DISCUSSION`, any open per-speaker VAD windows SHALL be abandoned: no partial `SpeechEvent` is sent for an utterance whose `vad_speech_ended` had not yet been observed. The restart MUST be logged as `voice_ingest_restart` with the abandoned `speaker_user_id`s. The worker reconnects to Discord and rebuilds its NPC-registry view from Master before resuming capture.

#### Scenario: In-flight VAD is abandoned across restart
- **WHEN** voice-ingest crashes mid-utterance for a human speaker and restarts
- **THEN** no `SpeechEvent` is sent for that abandoned utterance, and the restart is logged with `event=voice_ingest_restart` and the affected speaker's `user_id`
