# Design — Master + Separated NPC Voice Bots + Human STT

This design backs the proposal `add-master-npc-voice-stt`. It expands the spec deltas (`speech-event-bus`, `voice-ingest`, `npc-voice-pipeline`, plus modified `day-discussion`, `llm-seats`, `discord-integration`) into the architecture, contracts, and integration choices that the apply phase will execute against.

## Concerns

The change is decomposed into seven user-facing concerns. Each is independently reviewable, and most can be implemented in parallel once their foundational contracts (the SpeechEvent shape, the WS protocol, the NPC registry view) are agreed.

| ID | Concern | Problem it resolves |
|----|---------|---------------------|
| C1 | **Unified utterance bus (`speech-event-bus`)** | Today, public utterances live only as `PLAYER_SPEECH` log entries that are tightly coupled to the round-based LLM batch. Voice and reactive flows need a single canonical record of "who said what when," queryable across modes, rebuildable on restart. |
| C2 | **Public discussion derivation (`PublicDiscussionState`)** | Master-side reactive arbitration needs a stable code-derived view of who has CO'd, who is silent, etc. — not an LLM-summarized blob. The state must be deterministic from the event log so debugging and tests are sound. |
| C3 | **Human voice ingestion (`voice-ingest`)** | Humans cannot today participate by voice. We need a worker that listens to the Discord VC, runs VAD + STT, and emits `SpeechEvent(source=voice_stt)` — while reliably excluding NPC TTS audio so the model never feeds itself. |
| C4 | **NPC bot worker (`wolfbot-npc`)** | Each NPC needs its own Discord identity to be visibly distinct in VC and to play its own TTS voice. The bot must be a separable process with no game-state authority. |
| C5 | **Master ↔ NPC speech protocol** | Master must drive who speaks, when, with what context, and gate all NPC playback through explicit authorization. The protocol must reject stale results so a slow xAI response cannot leak speech into a phase that already advanced. |
| C6 | **Pluggable discussion mode (`day-discussion`)** | We need to introduce `reactive_voice` without breaking existing `rounds` games. The same code path must serve both modes, with a single mode-fixed-per-game switch via `LLM_DISCUSSION_MODE`. |
| C7 | **Cross-component observability** | Failures span STT / Master / NPC bot / TTS / VC. Without unified structured logs and a per-phase summary, regressions are invisible. |

## State / Lifecycle

### Canonical state (authoritative; written exactly once)

| State | Owner | Persistence | Notes |
|-------|-------|-------------|-------|
| `Game`, `Seat`, `Player`, `Vote`, `NightAction`, `LogEntry` | Master | `wolfbot.db` (existing) | Unchanged by this change. |
| `SpeechEvent` row | Master (sole writer) | `speech_events` table (new) | Inserted by Master regardless of utterance origin (voice-ingest sends payload over ingestion endpoint; NPC bot sends `SpeakResult` over WS). |
| `npc_speak_requests` row | Master | `npc_speak_requests` (new) | Inserted at `SpeakArbiter` dispatch time. |
| `npc_speak_results` row | Master | `npc_speak_results` (new) | Inserted on `SpeakResult` arrival or recovery sweep. |
| `npc_playback_events` row | Master | `npc_playback_events` (new) | An **open** row (with `finished_at` NULL) is inserted when `PlaybackAuthorized` is issued — which happens **immediately after** Master accepts a `SpeakResult` and writes the `SpeechEvent`, **before** TTS begins. TTS and playback both run inside this authorized window. The row carries `tts_outcome` (`success` / `failed` / NULL while pending), `tts_duration_ms`, and `tts_failure_reason` columns — updated when `tts_finished` or `tts_failed` arrives from the NPC bot. If TTS fails, the row is **closed** immediately (`finished_at` + `tts_outcome=failed`); no audio playback occurs. If TTS succeeds, `tts_outcome` is updated to `success` and the row remains open until `playback_finished` / `playback_failed` / `playback_deadline_ms` timeout, at which point `finished_at` and `outcome` are set. On `master_restart`, all rows with `finished_at IS NULL` are closed with `failure_reason=master_restart`. |
| `llm_speech_counts` (existing) | Master | existing table | Used only in `rounds` mode. |

### Derived state (rebuildable; never the source of truth)

| State | Derivation | Lifetime |
|-------|-----------|----------|
| `PublicDiscussionState` | Pure fold over `speech_events` rows for the current `phase_id`. The alive-seat baseline is persisted as a **sentinel `SpeechEvent`** with `source=phase_baseline` inserted at phase start; the sentinel's `alive_seat_nos_json` column stores the `frozenset[int]` serialized as a JSON array. `silent_seats` = sentinel's `alive_seat_nos` minus seats with ≥1 non-sentinel `SpeechEvent` in the phase. This makes the fold self-contained — rebuild reads only `speech_events`, never the `seats` table. | In-memory per active game; rebuilt on Master restart by replaying the fold from `speech_events` alone (the sentinel provides the baseline). |
| NPC registry view in voice-ingest | Pushed from Master via WS `registry_snapshot` on connect and `registry_update` on changes. | In-memory per voice-ingest process; re-delivered on reconnect. |
| Open serial-speech gate (`human-speaking`, `npc-playing`) | `human-speaking`: set by `vad_speech_started` from voice-ingest WS, cleared on `vad_speech_ended` + finalized payload/failure (or `vad_finalization_timeout_ms`). `npc-playing`: computed from `npc_playback_events` rows with `finished_at IS NULL` (the open row inserted at `PlaybackAuthorized` time, **before TTS begins** — the gate covers both TTS synthesis and audio playback). | In-memory on Master; cleared to empty on restart (voice-ingest re-establishes via next `vad_speech_started`; open playback rows are closed with `failure_reason=master_restart` during the recovery sweep). |

### Lifecycle boundaries

- **Game lifecycle** is unchanged: `LOBBY → SETUP → NIGHT_0 → DAY_DISCUSSION ↔ DAY_VOTE … → GAME_OVER`. The new behavior plugs into all public speech phases (`DAY_DISCUSSION`, `DAY_RUNOFF_SPEECH`) — `LLM_DISCUSSION_MODE` selects the NPC speech strategy for these phases, and `SpeechEvent` capture + `PublicDiscussionState` derivation + `discussion_phase_summary` emission apply uniformly to every public speech phase.
- **`SpeakRequest` lifecycle**: created → dispatched over WS → result arrives (accepted/declined/error) → Master validates utterance (length cap, phase freshness) → if accepted: Master writes `SpeechEvent`, issues `PlaybackAuthorized` immediately (inserts an **open** `npc_playback_events` row) → NPC performs TTS inside the authorized window → `tts_finished` or `tts_failed` reported to Master (updates the open row) → if TTS succeeded (`tts_outcome=success`): NPC plays audio → `playback_finished` OR `playback_deadline_ms` timeout → the open row is **closed** with `finished_at` and `outcome`. If TTS failed (`tts_outcome=failed`): the open row is **closed** immediately; no audio playback occurs. The serial-speech gate (`npc-playing`) is held from `PlaybackAuthorized` through both TTS and playback, so synthesis time is covered by the gate and concurrent NPC turns cannot prepare TTS in parallel.
- **VAD window lifecycle**: `vad_speech_started` → packets buffered → `vad_speech_ended` → STT call → either `SpeechEvent(source=voice_stt)` written OR drop with structured-log evidence.
- **Process lifecycle**: Master, voice-ingest, and each NPC bot can restart independently; in-flight WS-protocol state at the restarted boundary is dropped (see "Integration Points").

### Persistence-sensitive state

`speech_events` is the only new state that must survive restart for correctness — `PublicDiscussionState` is rebuilt from it. `npc_speak_*` and `npc_playback_events` are audit trails (queryable forever for postmortem) but the live arbiter does not depend on them after a restart; pending entries are simply marked rejected/failed during the recovery sweep.

## Contracts / Interfaces

### Inter-process contracts (new)

**Master ↔ NPC bot (WebSocket, localhost-only)**

```
NPC → Master:   npc_register {npc_id, discord_bot_user_id, supported_voices[], version}
Master → NPC:   npc_registered {assigned_seat, game_id, phase_id}
NPC → Master:   heartbeat {npc_id, ts}
Master → NPC:   logic_packet {packet_id, phase_id, recipient_npc_id, public_state_summary,
                              logic_candidates[{id, claim, support[], counter[]}],
                              pressure{seat→float}, expires_at_ms}
Master → NPC:   speak_request {request_id, phase_id, npc_id, seat_no, logic_packet_id,
                               suggested_intent, max_chars, max_duration_ms,
                               priority, expires_at_ms}
NPC → Master:   speak_result {request_id, npc_id, phase_id, status, text?, used_logic_ids[],
                              intent?, estimated_duration_ms?, failure_reason?}
Master → NPC:   playback_authorized {request_id, npc_id, status=authorized,
                                     speech_event_id, playback_deadline_ms}
Master → NPC:   playback_rejected {request_id, npc_id, status=rejected, failure_reason}
NPC → Master:   tts_finished {request_id, npc_id, tts_duration_ms, audio_size_bytes}
NPC → Master:   tts_failed {request_id, npc_id, failure_reason}
NPC → Master:   playback_finished {request_id, npc_id, started_at_ms, finished_at_ms}
NPC → Master:   playback_failed {request_id, npc_id, failure_reason}
```

All messages share a common envelope `{type, ts, trace_id}` and are JSON-encoded UTF-8. Authentication: a single `MASTER_NPC_PSK` is presented in the connect handshake; mismatch → connection refused.

**voice-ingest → Master (WebSocket, localhost-only)**

This contract fully specifies the voice-ingest control plane: **transport** (WebSocket, chosen below), **VAD lifecycle signaling** (`vad_speech_started` / `vad_speech_ended` with `segment_id` correlation), **finalized speech delivery** (`speech_event_payload` / `stt_failed`), **NPC registry delivery** (`registry_snapshot` / `registry_update`), **fail-closed behavior** when registry is unavailable, **serial-speech gate semantics** during control-plane failures, **restart semantics** for open VAD windows, and **`failure_reason` ownership** between voice-ingest and Master. Each sub-decision is detailed in a dedicated paragraph below.

Transport choice: WebSocket (same `websockets` library as the NPC channel). HTTP was considered but rejected because VAD lifecycle events (`vad_speech_started`, `vad_speech_ended`) require low-latency push from voice-ingest to Master to drive the serial-speech gate; a polling or request-response model adds unacceptable latency to gate transitions. The voice-ingest WS connection authenticates with the same `MASTER_NPC_PSK` mechanism as NPC bots.

```
voice-ingest → Master:  vad_speech_started {game_id, phase_id, speaker_discord_user_id, seat_no,
                                           segment_id, audio_start_ms, ts}
voice-ingest → Master:  vad_speech_ended   {game_id, phase_id, speaker_discord_user_id, seat_no,
                                           segment_id, audio_end_ms, ts}
voice-ingest → Master:  speech_event_payload {game_id, phase_id, seat_no, speaker_discord_user_id,
                                              segment_id, text, confidence, duration_ms,
                                              audio_start_ms, audio_end_ms, ts}
voice-ingest → Master:  stt_failed         {game_id, phase_id, speaker_discord_user_id, seat_no,
                                            segment_id, failure_reason, ts}
Master → voice-ingest:  registry_snapshot   {npc_user_ids: [discord_bot_user_id]}
Master → voice-ingest:  registry_update     {added: [discord_bot_user_id], removed: [discord_bot_user_id]}
voice-ingest → Master:  heartbeat           {ts}
```

All messages share the same common envelope `{type, ts, trace_id}` as the NPC channel.

**VAD lifecycle signaling to Master.** `vad_speech_started` and `vad_speech_ended` are sent as soon as the VAD engine detects transitions. Master uses these to set/clear the `human_currently_speaking` serial-speech gate — while a VAD window is open (after `vad_speech_started`, before `vad_speech_ended` + finalized `speech_event_payload` or `stt_failed` arrives), `SpeakArbiter` rejects NPC `SpeakRequest`s with `failure_reason=human_currently_speaking`. The gate clears when **both** `vad_speech_ended` has arrived **and** the corresponding `speech_event_payload` or `stt_failed` has been received (or a configurable `vad_finalization_timeout_ms` expires, closing the gate with a structured-log warning).

**Audio boundary fields and segment correlation.** Each VAD segment is assigned a unique `segment_id` by voice-ingest at `vad_speech_started` time; the same `segment_id` is carried through `vad_speech_ended`, `speech_event_payload`, and `stt_failed` so Master can correlate the full lifecycle. `vad_speech_started` carries `audio_start_ms` (milliseconds since the voice-ingest process joined the VC); `vad_speech_ended` carries `audio_end_ms`. Both values are echoed in `speech_event_payload` so Master can persist `audio_start_ms` and `audio_end_ms` directly into the `speech_events` row for `source=voice_stt` without inference. For `source=text` and `source=npc_generated`, `audio_start_ms` and `audio_end_ms` are NULL.

**`failure_reason` ownership.** voice-ingest owns STT-layer failure reasons (`stt_low_confidence`, `stt_provider_error`, `stt_timeout`). Master owns protocol- and game-layer failure reasons (`stale_phase`, `expired_request`, `human_currently_speaking`, `queue_busy`, `npc_offline`, `utterance_too_long`, `tts_timeout`, `tts_synthesis_error`, `playback_not_authorized`, `master_restart`, `npc_restart`, `voice_ingest_restart`, `voice_ingest_disconnect`, `discord_playback_error`, `npc_stt_discarded`). Each process emits structured-log events with only its own failure reasons; Master records voice-ingest-originated reasons verbatim when they arrive via WS (e.g. in `stt_failed` messages) but does not synthesize new STT-layer reasons.

**NPC registry delivery.** Master pushes `registry_snapshot` on voice-ingest connection and `registry_update` on NPC registration/deregistration changes. Voice-ingest no longer polls.

**Fail-closed on registry unavailability.** If voice-ingest has not yet received a `registry_snapshot` (initial connect not complete) or the WS connection to Master is down, voice-ingest operates in **fail-closed mode**: it treats the NPC user-ID set as **empty**, meaning all audio (including any NPC TTS) is processed through VAD/STT. This is safe because Master applies a **NPC-originated STT discard** guard: when writing a `SpeechEvent` from a `speech_event_payload`, Master checks if `speaker_discord_user_id` belongs to a registered NPC in the `NpcRegistry`; if so, the event is discarded with structured-log event `npc_stt_discarded` and no `SpeechEvent` row is written. This guarantees the "never feed NPC TTS into STT" invariant even during fail-closed operation — the worst case is wasted STT compute, not corrupted game state. The alternative (fail-open = drop all audio) would silence humans during transient Master outages, which is worse.

**Serial-speech gate during control-plane failures.** When the voice-ingest WS connection to Master is down, Master cannot receive `vad_speech_started` / `vad_speech_ended`. During this window the `human_currently_speaking` gate remains in whichever state it was left (cleared on disconnect per the restart semantics below). `SpeakArbiter` continues to schedule NPC speech; if a human is actually speaking but the gate is unset (because voice-ingest disconnected), the NPC may speak concurrently — this is an accepted degradation under connection loss. The reconnection path is self-healing: voice-ingest's next `vad_speech_started` after reconnection re-establishes the gate. No manual intervention is required.

**Restart semantics for open VAD windows.** On voice-ingest restart, all open VAD windows are abandoned — no `vad_speech_ended` is sent for windows from the prior process. On Master restart, all tracked `human_currently_speaking` gates are cleared (Master starts with an empty gate set); if a VAD window is genuinely still open, voice-ingest's next `vad_speech_started` re-establishes it after reconnection. On Master-side WS disconnect from voice-ingest, Master clears all `human_currently_speaking` gates associated with that voice-ingest connection, logging `failure_reason=voice_ingest_disconnect`.

### In-process contracts (Protocol-based, for testability)

| Protocol | Implementations (real / fake) | Owner |
|----------|-------------------------------|-------|
| `SpeechEventStore` | `SqliteSpeechEventStore` / `FakeSpeechEventStore` | Master |
| `PublicDiscussionStateBuilder` | `DefaultBuilder` (pure code) / fake n/a (build is pure) | Master |
| `NpcRegistry` | `InMemoryNpcRegistry` / `FakeNpcRegistry` | Master |
| `SpeakArbiter` | `DefaultSpeakArbiter` / `FakeArbiter` (test override) | Master |
| `MasterWsServer` | `WebsocketsMasterWsServer` / `FakeMasterWsServer` | Master |
| `VoiceIngestClient` (master-side) | `WebsocketsVoiceIngestClient` / `FakeVoiceIngestClient` | voice-ingest |
| `SttService` | `GeminiSttService` / `FakeSttService` | voice-ingest |
| `VadEngine` | `WebrtcVadEngine` (or chosen library) / fake n/a | voice-ingest |
| `MasterClient` (npc-side) | `WebsocketsMasterClient` / `FakeMasterClient` | NPC bot |
| `TtsService` | `GoogleTtsService` (cost-minimized default) / `FakeTtsService` | NPC bot |
| `VoicePlayback` | `DiscordVoicePlayback` / `FakeVoicePlayback` | NPC bot |
| `NpcGenerator` | `XaiNpcGenerator` (reuses existing decider) / `FakeNpcGenerator` | NPC bot |

The existing `DiscordAdapter`, `LLMAdapter`, `LLMActionDecider`, `MessagePoster`, `WakeSink` Protocols are preserved — `LLMAdapter` is unchanged for `rounds` mode; `reactive_voice` mode goes through the new `SpeakArbiter` instead.

### Human text ingestion and PLAYER_SPEECH emission (R1-F01)

**Human text → SpeechEvent.** In both discussion modes, human messages sent to the main text channel during any public speech phase (`DAY_DISCUSSION`, `DAY_RUNOFF_SPEECH`) are captured by `WolfCog` (existing Discord message listener on Master) and written as `SpeechEvent(source=text, speaker_kind=human)` via `SpeechEventStore`. No new inter-process contract is needed — this is an in-process path on Master.

**SpeechEvent → PLAYER_SPEECH LogEntry + channel post.** Every accepted `SpeechEvent` — regardless of source (`text`, `voice_stt`, `npc_generated`) — triggers two side-effects on Master:
1. A `LogEntry(kind="PLAYER_SPEECH")` is appended to the existing game log (preserving the public-log contract for both modes).
2. The utterance text is posted to the main text channel via `MessagePoster` (for `voice_stt` and `npc_generated` sources this makes the utterance visible to text-only observers; for `text` source the original Discord message already serves as the channel post, so step 2 is skipped to avoid duplication).

This is implemented as a `SpeechEventStore` post-write hook (or inline in the discussion service write path) so the contract is uniform across all ingestion origins: voice-ingest POST, NPC `SpeakResult` acceptance, rounds-mode backfill, and human text capture.

**Reactive `SpeakResult` utterance validation.** Before writing a `SpeechEvent` and authorizing playback, Master validates the returned utterance text in `SpeakResult`:
- **Length cap**: In `reactive_voice` mode, `len(text) > 80` → reject with `failure_reason=utterance_too_long`; no `SpeechEvent` is written and no `PlaybackAuthorized` is issued. In `rounds` mode (if `SpeakResult` were ever used there), the cap is 300. The rejected result is recorded in `npc_speak_results` with `status=rejected` and the failure reason.
- **Phase freshness**: `phase_id` and `request_id` must match the current active phase and an outstanding request (existing check).
- Validation runs **before** `SpeechEvent` insertion and **before** `PlaybackAuthorized`, so an over-length utterance never enters the public discussion state or reaches Discord playback.

## Persistence / Ownership

### Data ownership boundaries

- **Master is the sole writer** to all SQLite tables (`speech_events`, `npc_speak_*`, `npc_playback_events`, plus existing tables). voice-ingest and NPC bots never touch `wolfbot.db`.
- **NPC bots own their own Discord identity** (token, voice channel join, audio playback). Master never plays audio.
- **voice-ingest owns audio capture**. NPC bots and Master never read incoming voice packets.

### Storage mechanisms

- All persistent state stays in `wolfbot.db` (SQLite via aiosqlite). Schema changes are additive (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN` guarded by `PRAGMA table_info`).
- The TTS audio cache (used by NPC bots) is in-memory per process for the MVP; cache key = `provider + voice_id + sha256(text) + speed + pitch`. A persistent cache is out of scope.
- WebSocket sessions, heartbeat tracking, and arbiter gates are in-memory on Master. After restart, Master starts with empty session state and reconciles existing DB rows via the recovery sweep.

### Artifact ownership

- `proposal.md`, `design.md`, `tasks.md`, the change's `specs/**/*.md` deltas — owned by this change directory.
- Baseline `openspec/specs/<capability>/spec.md` files — shared across changes; will be merged-in form on archive (per OpenSpec convention).

## Integration Points

### External systems

- **Discord (gateway + voice + REST)** — Master, every NPC bot, voice-ingest each maintain their own Discord client connection. Slash commands stay on Master.
- **xAI Grok endpoint** — used by Master's existing `LLMAdapter` (rounds mode) and by each NPC bot (reactive_voice). No change to the `LLMAction` schema or to `response_format` strictness.
- **Gemini API (audio input)** — invoked by voice-ingest for STT only. Out of band from xAI traffic.
- **TTS provider (cost-minimized default: Google Cloud TTS Standard voices)** — invoked by each NPC bot. Provider is pluggable via `TTS_PROVIDER` / `TTS_VOICE_ID`.

### Cross-layer dependency points

- voice-ingest depends on Master's NPC registry (delivered via WS `registry_snapshot` / `registry_update`) to know which Discord user IDs to drop. If the WS connection is down or no snapshot has been received, voice-ingest operates fail-closed (empty NPC set → all audio processed; see "voice-ingest → Master" contract above for rationale).
- The serial-speech gate on Master couples voice-ingest VAD lifecycle events (`vad_speech_started`, `vad_speech_ended`) with NPC playback windows. The gate is the single integration point where these two streams meet; gate transitions and timeout behavior are specified in the voice-ingest WS contract above.
- `SpeakArbiter` reads `PublicDiscussionState`, which is built from `speech_events`, which is fed by both voice-ingest and NPC bots. This is a closed loop and is the heart of reactive_voice.

### Regen / retry / save / restore boundaries

- xAI errors retry within `tenacity` (existing). NPC bot failures bubble up as `speak_result.status=error` with `failure_reason`.
- STT errors and low-confidence results drop the utterance silently (no retry from voice-ingest's side — the human can simply speak again).
- TTS errors are recorded; the NPC stays silent for that turn but the game continues.
- Process restarts: in-flight WS state is dropped, audit-trail rows are finalized as rejected/failed by the recovery sweep, `PublicDiscussionState` is rebuilt from `speech_events`.

## Ordering / Dependency Notes

Implementation must proceed in the order below because later concerns rely on earlier contracts. Where parallel work is feasible, it is called out.

1. **Foundational (must come first):**
   - C1 `speech-event-bus` — defines the `SpeechEvent` shape, the `speech_events` table, the apply-event/rebuild functions. Everything downstream produces or consumes `SpeechEvent`.
   - C2 `PublicDiscussionState` — pure code, depends on C1's shape.

2. **Parallelizable once C1+C2 land:**
   - C3 `voice-ingest` worker (depends on C1 shape, on the Master ingestion endpoint, and on the NPC-registry read endpoint).
   - C4 `wolfbot-npc` worker skeleton (depends on the Master WS protocol shape from C5 below).
   - C5 Master WS server + `MasterLogicBuilder` + `SpeakArbiter` + new tables (depends on C2).
   - C6 `LLM_DISCUSSION_MODE=rounds` SpeechEvent writer (depends on C1; preserves observable rounds behavior).

3. **Integration (last):**
   - C6 `LLM_DISCUSSION_MODE=reactive_voice` end-to-end wiring — ties C3, C4, C5 together.
   - C7 structured-log standardization + `discussion_phase_summary` event — overlays everything; can begin in parallel with C5/C6.

4. **Operational ordering:**
   - DB migrations before any new code path that writes new tables.
   - Protocol Fakes alongside the real implementations (TDD-friendly).
   - Apply tasks should land in commits aligned to the concern boundaries above (one commit per concern when feasible).

## Completion Conditions

A concern is complete when its observable conditions and reviewable artifacts are all in place:

| Concern | Reviewable artifact | Observable condition |
|---------|---------------------|----------------------|
| C1 `speech-event-bus` | `domain/discussion.py` (`SpeechEvent`), `services/discussion_service.py`, `persistence/schema.py` migration block, unit tests | A `SpeechEvent` for any input source round-trips through write → rebuild → identical state. |
| C2 `PublicDiscussionState` | `domain/discussion.py` (state value object + `apply_speech_event` + `rebuild_public_state_from_events`), property tests | For a fixed event sequence and alive-seat baseline, the state is bitwise reproducible from a fresh fold; co_claims and silent_seats match the specified rules; silent_seats correctly excludes dead seats. |
| C3 `voice-ingest` | `services/voice_ingest_service.py`, `services/stt_service.py`, NPC-registry read client, ingestion HTTP client, unit tests with `FakeSttService` | A scripted human VAD segment produces exactly one `SpeechEvent(source=voice_stt)` on Master; an NPC packet produces zero `SpeechEvent`s; below-threshold STT produces zero `SpeechEvent`s plus the expected log events. |
| C4 `wolfbot-npc` worker | `npc_bot_main.py`, `services/npc_client.py`, `services/npc_speech_service.py`, `services/tts_service.py`, `services/voice_playback_service.py`, integration test with `FakeMasterWsServer` + `FakeTtsService` + `FakeVoicePlayback` | The NPC bot registers, receives a `SpeakRequest`, returns `SpeakResult`, waits for `PlaybackAuthorized` before calling TTS, synthesizes and plays only within the authorized window, reports `tts_finished`/`tts_failed` and `playback_finished`/`playback_failed`. |
| C5 Master speech arbitration | `services/master_logic_service.py`, `services/speak_arbiter.py`, `services/npc_registry.py`, `services/master_ws_server.py`, three new tables, recovery sweep, unit + integration tests | An end-to-end reactive_voice fixture (Master + Fake NPC + Fake voice-ingest) produces correct serial speech, drops stale results, and rebuilds `PublicDiscussionState` after a simulated Master restart. |
| C6 `day-discussion` mode plumbing | `LLM_DISCUSSION_MODE` setting in `config.py`, dispatcher in `game_service`, regression tests for `rounds` mode (must stay green) | Existing `rounds` tests still pass; a new `reactive_voice` integration test exercises the new path; mode is observably fixed for a game's lifetime. |
| C7 Observability | Logging helper in shared module, every new service emits the required fields, `discussion_phase_summary` event at every phase end, structured-log fixture tests | Every spec-listed log event appears in the test logs with the required fields; `discussion_phase_summary` includes separate `tts_success` / `tts_failed` counts (from `npc_playback_events.tts_outcome`) and `playback_success` / `playback_failed` counts; speech-event counts equal the number of `SpeechEvent` rows for the phase under test. |

## Accepted Spec Conflicts

| id | capability | delta_clause | baseline_clause | rationale | accepted_at |
|----|-----------|--------------|-----------------|-----------|-------------|
| AC1 | llm-seats | Under `LLM_DISCUSSION_MODE=reactive_voice`, utterances MUST be ≤80 characters and MAY be shorter. | Every LLM-generated utterance SHALL be Japanese, between 80 and 300 characters inclusive. | Reactive voice mode favors short reactive interjections (single barbs, ≤80 chars) over long monologues; the user explicitly approved relaxing the lower bound under this mode during clarify. The 80–300 contract is preserved unchanged for `rounds` mode. | 2026-04-26T04:50:00Z |
| AC2 | speech-event-bus | A sentinel row with `source=phase_baseline` and `alive_seat_nos_json` is inserted into `speech_events` at the start of each public speech phase to provide the alive-seat baseline for `PublicDiscussionState` rebuild. | `SpeechEvent` is defined as a unified public-utterance contract with `source` values `voice_stt`, `text`, `npc_generated`. | The sentinel makes the `PublicDiscussionState` fold self-contained (rebuild reads only `speech_events`, never the `seats` table), which is critical for correctness on Master restart. The sentinel is excluded from public-log emission (no `PLAYER_SPEECH` LogEntry, no channel post) and from all downstream consumer counts; consumers filter on `source != phase_baseline`. | 2026-04-26T15:00:00Z |
