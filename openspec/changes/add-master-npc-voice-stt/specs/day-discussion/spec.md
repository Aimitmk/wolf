## MODIFIED Requirements

### Requirement: LLM seats speak twice per discussion phase

For every `DAY_DISCUSSION` phase **when `LLM_DISCUSSION_MODE=rounds`**, each living LLM seat SHALL emit exactly two structured speech utterances. Progress for each seat MUST be tracked in the `llm_speech_counts.discussion_rounds_done` column so that an interrupted phase (process restart, deadline-driven advance, host `/wolf force-skip`) does not cause the same round to be repeated when execution resumes. **When `LLM_DISCUSSION_MODE=reactive_voice`, this fixed two-round contract does NOT apply** — speech is instead driven by `SpeakArbiter` against `PublicDiscussionState`, and the count of utterances per seat is dynamic (zero or more).

#### Scenario: Two utterances per LLM seat in a fresh phase (rounds mode)
- **WHEN** `DAY_DISCUSSION` starts for `day_number = 1` with 4 living LLM seats and the deadline is far enough in the future, with `LLM_DISCUSSION_MODE=rounds`
- **THEN** the system schedules two utterances per LLM seat (8 total), each producing a `PLAYER_SPEECH` `LogEntry`

#### Scenario: Resume after restart in mid-phase (rounds mode)
- **WHEN** the bot restarts during `DAY_DISCUSSION` after one round of LLM speech has already been recorded for an LLM seat under `LLM_DISCUSSION_MODE=rounds`
- **AND** that seat's `llm_speech_counts.discussion_rounds_done` is `1`
- **THEN** on resumption the system schedules only the remaining round for that seat, not both rounds

#### Scenario: reactive_voice mode does not use llm_speech_counts.discussion_rounds_done
- **WHEN** the bot runs with `LLM_DISCUSSION_MODE=reactive_voice`
- **THEN** the speech-batching path is not invoked; `llm_speech_counts.discussion_rounds_done` is not incremented and is not used to drive scheduling

### Requirement: Discussion mode is configurable

The day-discussion engine SHALL accept a runtime mode setting via the `LLM_DISCUSSION_MODE` environment variable that selects between two strategies. The default mode `rounds` MUST run the existing two-round LLM batching with the existing 80–300-character utterance cap. The alternative mode `reactive_voice` MUST replace the LLM-speech batching with Master-arbitrated NPC speech driven by `PublicDiscussionState`, with a hard `max_chars=80` per utterance. **The mode is fixed for the lifetime of a single game** (no mid-game switching). Both modes MUST preserve the public-log contract — every utterance, regardless of mode, is emitted as a `PLAYER_SPEECH` `LogEntry`. Both modes MUST also write a `SpeechEvent` row for every utterance (human text, human voice via STT, NPC) and update an in-memory `PublicDiscussionState` derived from those rows.

#### Scenario: Default mode is rounds
- **WHEN** the bot starts without `LLM_DISCUSSION_MODE` set
- **THEN** `DAY_DISCUSSION` runs the two-round LLM batching described in this spec, with the existing 80–300-character cap

#### Scenario: reactive_voice mode replaces LLM batching
- **WHEN** the bot starts with `LLM_DISCUSSION_MODE=reactive_voice`
- **THEN** `DAY_DISCUSSION` does not invoke the round-based LLM batch; `SpeakArbiter` drives NPC speech instead, with `max_chars=80` per utterance

#### Scenario: Both modes preserve PLAYER_SPEECH
- **WHEN** an utterance is recorded under either mode
- **THEN** a `LogEntry(kind="PLAYER_SPEECH", actor_seat=<seat_no>, text=<utterance>)` is emitted to the public log path and posted to the main channel

#### Scenario: Both modes write SpeechEvent
- **WHEN** an utterance is recorded under either mode
- **THEN** a `SpeechEvent` row is inserted into `speech_events` with the appropriate `source` (`text`, `voice_stt`, or `npc_generated`) and `speaker_kind` (`human` or `npc`)

#### Scenario: Mode is fixed for a game
- **WHEN** a game starts in mode `M` and the host or operator changes the `LLM_DISCUSSION_MODE` environment variable mid-game
- **THEN** the running game continues to use mode `M`; the new value applies only to subsequent games

## ADDED Requirements

### Requirement: Discussion phase end emits a discussion_phase_summary log event

At the end of every `DAY_DISCUSSION` phase (and `DAY_RUNOFF_SPEECH` if applicable), regardless of `LLM_DISCUSSION_MODE`, the system SHALL emit a structured log event with `event=discussion_phase_summary` summarizing what happened during that phase. The event MUST include at least: `game_id`, `phase_id`, `mode`, `speech_events_total`, `human_speech_events`, `npc_speech_events`, and (when applicable) `stt_success`, `stt_failed`, `logic_packets_built`, `speak_requests_sent`, `speak_results_accepted`, `speak_results_rejected`, `playback_authorized`, `tts_success`, `tts_failed`, `playback_success`, `playback_failed`, `stale_dropped`.

#### Scenario: rounds mode emits a summary
- **WHEN** a `DAY_DISCUSSION` phase ends under `LLM_DISCUSSION_MODE=rounds`
- **THEN** a structured log event with `event=discussion_phase_summary`, `mode=rounds`, the count of human and NPC `SpeechEvent` rows for the phase, and zeros for the reactive-voice fields is emitted

#### Scenario: reactive_voice mode emits a richer summary
- **WHEN** a `DAY_DISCUSSION` phase ends under `LLM_DISCUSSION_MODE=reactive_voice`
- **THEN** the summary log event includes the full reactive-voice telemetry (logic packets, speak requests/results, playback outcomes, stale drops) along with the speech counts
