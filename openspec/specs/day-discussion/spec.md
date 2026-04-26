# day-discussion Specification

## Purpose

Drives the `DAY_DISCUSSION` and `DAY_RUNOFF_SPEECH` phases of a 9-player Werewolf game. Coordinates day-time speech for both human seats (free Discord text) and LLM seats (batched generated speech), publishes utterances as `PLAYER_SPEECH` log entries, and tracks per-LLM-seat speech progress so that restarts and host force-skips resume correctly without double-posting.

## Requirements

### Requirement: Day discussion duration is fixed per day number

`DAY_DISCUSSION` SHALL run for a deterministic duration depending on `Game.day_number`, computed by `domain.rules.day_discussion_duration(day)`. The duration MUST be 300 seconds on day 1, 240 seconds on day 2, and 180 seconds on day 3 and later.

#### Scenario: Day 1 discussion length
- **WHEN** the state machine plans the transition into `DAY_DISCUSSION` for `day_number = 1`
- **THEN** the resulting `Transition.deadline_epoch` is exactly 300 seconds after the planning `now_epoch`

#### Scenario: Day 3+ discussion length
- **WHEN** the state machine plans the transition into `DAY_DISCUSSION` for `day_number >= 3`
- **THEN** the resulting `Transition.deadline_epoch` is exactly 180 seconds after the planning `now_epoch`

### Requirement: LLM seats speak twice per discussion phase

For every `DAY_DISCUSSION` phase, each living LLM seat SHALL emit exactly two structured speech utterances. Progress for each seat MUST be tracked in the `llm_speech_counts.discussion_rounds_done` column so that an interrupted phase (process restart, deadline-driven advance, host `/wolf force-skip`) does not cause the same round to be repeated when execution resumes.

#### Scenario: Two utterances per LLM seat in a fresh phase
- **WHEN** `DAY_DISCUSSION` starts for `day_number = 1` with 4 living LLM seats and the deadline is far enough in the future
- **THEN** the system schedules two utterances per LLM seat (8 total), each producing a `PLAYER_SPEECH` `LogEntry`

#### Scenario: Resume after restart in mid-phase
- **WHEN** the bot restarts during `DAY_DISCUSSION` after one round of LLM speech has already been recorded for an LLM seat
- **AND** that seat's `llm_speech_counts.discussion_rounds_done` is `1`
- **THEN** on resumption the system schedules only the remaining round for that seat, not both rounds

### Requirement: All speech is published as PLAYER_SPEECH log entries

Every successful day-discussion utterance — whether produced by a human via Discord text or by an LLM seat via the structured-output pipeline — SHALL be persisted as a `LogEntry` with `kind = "PLAYER_SPEECH"`, attributed to the originating `seat_no` and `display_name`, and visible in the public main-text channel.

#### Scenario: LLM speech is logged
- **WHEN** an LLM seat's `submit_llm_discussion_rounds` task produces an utterance
- **THEN** a `LogEntry(kind="PLAYER_SPEECH", actor_seat=<seat_no>, text=<utterance>)` is appended via the public-log path and posted to the main channel

#### Scenario: Human speech is logged
- **WHEN** a human player sends a message in the main text channel during `DAY_DISCUSSION`
- **THEN** the bot records the message as a `PLAYER_SPEECH` `LogEntry` for the speaker's seat

### Requirement: LLM submissions never block the advance loop

`LLMAdapter.submit_llm_discussion_rounds` and the runoff-speech equivalent SHALL schedule background `asyncio` tasks and return without awaiting the xAI round-trip. The `GameService.advance(game_id)` call site MUST NOT await LLM completion, and a slow xAI response MUST NOT delay deadline detection or phase transitions.

#### Scenario: advance() returns before LLM responses arrive
- **WHEN** `GameService.advance` enters `DAY_DISCUSSION` and dispatches LLM submissions
- **THEN** the call returns once all background tasks are scheduled, regardless of xAI latency

#### Scenario: Stale background task detects phase change
- **WHEN** a background LLM submission task is in flight and a `/wolf force-skip` advances the phase before the task completes
- **THEN** the task re-loads the game, observes the changed `phase`, `day_number`, or non-null `ended_at`, and silently aborts without writing a `PLAYER_SPEECH` log

### Requirement: Concurrent LLM seats run in parallel; wolf night-chat stays serial

Within a single discussion or runoff-speech batch, per-seat LLM work SHALL run concurrently via `asyncio.gather` so that multiple LLM seats hit xAI simultaneously rather than serially. The wolf night-chat coordination, however, MUST remain serial because later wolves rely on reading earlier wolves' messages from the shared wolves channel.

#### Scenario: Discussion batch runs in parallel
- **WHEN** four LLM seats are alive and `submit_llm_discussion_rounds` schedules a round
- **THEN** the four xAI requests are issued concurrently rather than one after another

#### Scenario: Wolf night-chat is serial
- **WHEN** multiple LLM wolves are alive during a NIGHT phase
- **THEN** their wolf-channel utterances are produced in seat order, each later wolf seeing the prior wolf's messages

### Requirement: Runoff speeches occur only when LLM candidates are tied

`DAY_RUNOFF_SPEECH` SHALL be entered only when `DAY_VOTE` produces a tie that includes at least one LLM-controlled candidate. In that phase, each tied LLM candidate MUST emit exactly one speech utterance, tracked by `llm_speech_counts.runoff_speech_done`.

#### Scenario: Tie with LLM candidate triggers runoff speeches
- **WHEN** `DAY_VOTE` resolves with two seats tied and one of them is an LLM seat
- **THEN** the state machine transitions to `DAY_RUNOFF_SPEECH`, and the LLM seat produces exactly one runoff speech before `DAY_RUNOFF` begins

#### Scenario: All-human tie skips runoff speech phase
- **WHEN** `DAY_VOTE` resolves with two human seats tied and no LLM candidate
- **THEN** the state machine transitions directly to `DAY_RUNOFF`, skipping `DAY_RUNOFF_SPEECH`

### Requirement: LLM submissions re-validate phase before each per-seat write

Inside every background LLM submission task, the system SHALL re-load the game and re-check `phase`, `day_number`, and `ended_at` immediately before writing each per-seat `PLAYER_SPEECH` log. A mismatch on any field MUST cause the per-seat write to be skipped.

#### Scenario: Phase change mid-batch drops remaining seats
- **WHEN** a discussion batch has dispatched per-seat tasks and `/wolf force-skip` advances to `DAY_VOTE` before all tasks complete
- **THEN** any per-seat task that observes the new phase aborts before posting its utterance, and no late `PLAYER_SPEECH` is written for that day's discussion

### Requirement: Discussion mode is configurable

The day-discussion engine SHALL accept a runtime mode setting (e.g. via the `LLM_DISCUSSION_MODE` environment variable) that selects between alternative discussion strategies. The default mode MUST be the round-based strategy described above; alternative modes MAY replace the LLM-speech batching behavior while preserving the public-log contract (`PLAYER_SPEECH` entries) and the deadline contract.

#### Scenario: Default mode runs fixed-round discussion
- **WHEN** the bot starts without `LLM_DISCUSSION_MODE` set
- **THEN** `DAY_DISCUSSION` runs the two-round LLM batching described in this spec

#### Scenario: Alternative mode preserves the public-log contract
- **WHEN** an alternative discussion mode is selected
- **THEN** every recorded utterance is still emitted as a `PLAYER_SPEECH` `LogEntry` so downstream voting, recovery, and post-game replay are unaffected
