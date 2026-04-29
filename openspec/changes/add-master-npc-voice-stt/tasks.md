## 1. Speech Event Bus Foundation ✓

> Introduce the canonical SpeechEvent model, persistence, and service APIs that every discussion mode can write to and rebuild from.

- [x] 1.1 Define SpeechEvent domain types, source enums, and serialization rules.
- [x] 1.2 Add the additive speech_events migration and store/repository interfaces.
- [x] 1.3 Implement speech event write, read, and rebuild helpers for the master process.
- [x] 1.4 Add persistence and round-trip rebuild tests for SpeechEvent streams.

## 2. Public Discussion State Derivation ✓

> Build a deterministic PublicDiscussionState fold over speech_events for the active phase.

> Depends on: speech-event-bus-foundation

- [x] 2.1 Define the PublicDiscussionState value object and its deterministic fields.
- [x] 2.2 Implement apply_speech_event and rebuild_public_state_from_events as a pure fold.
- [x] 2.3 Encode co-claim and silence rules directly from event order without LLM summaries.
- [x] 2.4 Add regression and property-style tests that verify bitwise reproducibility from a fresh rebuild.

## 3. Rounds Mode SpeechEvent Backfill ✓

> Make the existing rounds discussion path write canonical SpeechEvents without changing current gameplay behavior.

> Depends on: speech-event-bus-foundation

- [x] 3.1 Route rounds-mode utterance generation through the SpeechEvent store.
- [x] 3.2 Preserve existing log entries and llm_speech_counts side effects for rounds mode.
- [x] 3.3 Add regression coverage that proves current rounds behavior remains unchanged.

## 4. Master Speech Control Surface ✓

> Add the master-owned protocol, endpoints, registry, and audit-table schema that external voice workers connect to.

> Depends on: public-discussion-state

- [x] 4.1 Define the shared WebSocket envelope, message schemas, and PSK handshake rules.
- [x] 4.2 Add additive migrations and repository hooks for npc_speak_requests, npc_speak_results, and npc_playback_events.
- [x] 4.3 Implement the master WebSocket server, NPC registration and heartbeat tracking, and localhost ingest and registry endpoints.
- [x] 4.4 Add fakes and protocol-focused tests for registration, heartbeats, and endpoint contracts.

## 5. Master Arbitration And Recovery ✓

> Build the master logic and arbiter that selects NPC speech, enforces serial gating, and finalizes stale or interrupted work.

> Depends on: public-discussion-state, master-speech-control-surface

- [x] 5.1 Build logic packet generation from PublicDiscussionState and seat pressure inputs.
- [x] 5.2 Implement SpeakArbiter dispatch, authorization and rejection flow, serial speech gates, and stale result handling.
- [x] 5.3 Finalize restart recovery rules for pending speak and playback rows plus in-memory gate rebuild.
- [x] 5.4 Add master-side tests for serial speech, stale responses, and restart rebuild behavior.

## 6. Voice Ingest Worker ✓

> Capture human voice, filter NPC audio, run VAD and STT, and send canonical speech events to the master.

> Depends on: speech-event-bus-foundation, master-speech-control-surface

- [x] 6.1 Add VAD, STT, and master-client protocols plus configuration for the voice-ingest worker.
- [x] 6.2 Implement NPC registry refresh and Discord packet filtering for bot user IDs.
- [x] 6.3 Implement VAD buffering, STT submission, and speech_event ingestion with explicit drop paths for low-confidence and error cases.
- [x] 6.4 Add tests for one-human-one-event, NPC exclusion, and low-confidence or error drops.

## 7. NPC Voice Worker ✓

> Run a per-NPC bot process that registers with the master, generates short utterances, and only plays after authorization.

> Depends on: master-speech-control-surface

- [x] 7.1 Implement the NPC-side master client with register, heartbeat, and request and result handling.
- [x] 7.2 Build short-utterance NPC generation, TTS synthesis, and in-memory audio cache services.
- [x] 7.3 Gate playback on explicit authorization and emit playback_finished and playback_failed events.
- [x] 7.4 Add integration tests with fake master, TTS, and playback services.

## 8. Reactive Voice Mode Plumbing ✓

> Wire reactive_voice as a mode-fixed day-discussion path without regressing rounds mode.

> Depends on: rounds-mode-speechevent-backfill, master-arbitration-and-recovery, voice-ingest-worker, npc-voice-worker

- [x] 8.1 Add LLM_DISCUSSION_MODE selection and freeze the chosen discussion mode for each game instance.
- [x] 8.2 Dispatch DAY_DISCUSSION between rounds and reactive_voice without changing rounds semantics.
- [x] 8.3 Wire master, voice-ingest, and NPC worker lifecycle management for reactive_voice sessions.
- [x] 8.4 Add end-to-end reactive_voice and rounds-regression tests.

## 9. Cross-Component Observability ✓

> Standardize structured logs and phase summaries across master, voice-ingest, and NPC workers for debugging and recovery.

> Depends on: master-arbitration-and-recovery, voice-ingest-worker, npc-voice-worker, reactive-voice-mode-plumbing

- [x] 9.1 Define a shared structured logging helper and required fields for trace, phase, request, seat, and source identifiers.
- [x] 9.2 Instrument master, voice-ingest, and NPC worker flows for drops, authorization, playback, and timeout events.
- [x] 9.3 Emit discussion_phase_summary at each phase end using speech_events and playback audit rows.
- [x] 9.4 Add log fixture tests that assert required events and per-phase speech counts.
