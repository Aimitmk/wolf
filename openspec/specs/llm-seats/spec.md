# llm-seats Specification

## Purpose

Specifies how unfilled seats in a 9-player Werewolf game are played by LLM-driven NPC personas backed by the xAI Grok endpoint. Defines the structured-output contract (`LLMAction`), the per-actor system-prompt composition pipeline (`prompt_builder`), the persona registry, the role-strategy isolation rules, and the seat-token resolver that maps generated `target_name` strings back to specific seats.

## Requirements

### Requirement: Unfilled seats are auto-backfilled by LLM-driven NPCs

When `/wolf start` runs and the lobby contains fewer than 9 human players, the system SHALL fill the remaining seats with LLM-controlled NPCs as part of the same atomic `claim_start_and_backfill` transaction. The number of LLM seats MUST equal `9 - <human seats>` so that the table always seats exactly 9 players.

#### Scenario: Six humans triggers three LLM seats
- **WHEN** `/wolf start` is invoked with exactly 6 human seats present in `LOBBY`
- **THEN** the start transaction inserts 3 additional LLM seats so the game enters `SETUP` with 9 seats total

#### Scenario: Solo lobby triggers eight LLM seats
- **WHEN** `/wolf start` is invoked with exactly 1 human seat in `LOBBY`
- **THEN** the start transaction inserts 8 additional LLM seats so the game enters `SETUP` with 9 seats total

### Requirement: Each LLM seat is bound to a unique persona

Each LLM seat SHALL be assigned a persona record from `wolfbot.llm.personas.PERSONAS_BY_KEY` carrying at minimum a katakana `display_name` prefixed by a distinguishing emoji, a free-form `style_guide` string for judgement and tone, and a structured `SpeechProfile` capturing speech reproduction (`first_person`, `self_reference_aliases`, `address_style`, `sentence_style`, `pause_style`, `signature_phrases`, `forbidden_overuse`, `narration_mode`). No two persona records in a single game MAY share the same `display_name`.

#### Scenario: Persona display_name is never duplicated within a game
- **WHEN** the start transaction selects personas for the LLM seats of a single game
- **THEN** every chosen persona's `display_name` is unique across that game's seat list

#### Scenario: Style and speech fields are isolated
- **WHEN** a persona record is read by the prompt builder
- **THEN** speech-reproduction data lives only in `speech_profile` and judgement/tone data lives only in `style_guide`; neither field carries content belonging to the other

### Requirement: LLM actions are returned as a strict structured JSON

LLM seats SHALL communicate with the xAI Grok endpoint using the OpenAI-compatible chat-completions API with `response_format` set to enforce the `LLMAction` JSON schema strictly. The schema MUST encode the four legal intents `speak`, `vote`, `night_action`, and `skip`, plus optional fields for `target_name` (a seat token), `text` (the utterance, if any), and reasoning metadata. Transient transport errors MUST be retried with exponential backoff via `tenacity`.

#### Scenario: Strict structured output is requested
- **WHEN** an LLM seat issues a request via `XAILLMActionDecider`
- **THEN** the request includes `response_format` configured to require the `LLMAction` JSON schema and rejects any unparseable response

#### Scenario: Transient errors retry with backoff
- **WHEN** the xAI endpoint returns a transient error (timeout, 5xx) on an LLMAction request
- **THEN** the request is retried using `tenacity` with exponential wait and a finite stop-after-attempt

### Requirement: System prompt is composed per actor

For every LLM call, the system prompt SHALL be composed dynamically by `prompt_builder.build_system_prompt` rather than loaded verbatim. The composition MUST layer (1) the markdown template at `src/wolfbot/prompts/llm_system_prompt.md` for base framing, output format, and hard invariants; (2) `_build_game_rules_block()` derived from `ROLE_DISTRIBUTION` and `VILLAGE_SIZE` plus shared CO-evaluation heuristics; (3) the role-specific entry from `_ROLE_STRATEGIES`; and (4) `_build_speech_profile_block(persona)` for the persona's speech reproduction.

#### Scenario: Game-rules block reflects canonical 9-player distribution
- **WHEN** the prompt builder runs for any LLM seat
- **THEN** the rendered game-rules block lists the role counts and village size derived from `ROLE_DISTRIBUTION` / `VILLAGE_SIZE` constants without duplicating the numbers in the markdown template

#### Scenario: Speech profile is rendered per persona
- **WHEN** the prompt builder runs for a seat whose persona uses `narration_mode = "silent_gesture"`
- **THEN** the speech-profile block renders gesture descriptions instead of a normal speech profile

### Requirement: Role strategies do not leak across roles

The role-strategy block injected into the system prompt SHALL include only the entry from `_ROLE_STRATEGIES` matching the actor's true role. Strategy text scoped to one role (e.g. wolf or madman fake-CO playbooks) MUST NOT appear in prompts built for any other role's actor.

#### Scenario: Wolf strategy never appears for a seer prompt
- **WHEN** the prompt builder composes a system prompt for a seat whose role is `seer`
- **THEN** the wolf and madman fake-CO strategy text is absent from the rendered prompt

#### Scenario: Villager strategy forbids self-CO declarations
- **WHEN** the prompt builder composes a system prompt for a seat whose role is `villager`
- **THEN** the rendered prompt forbids declaring `村人CO` / `素村CO` and equivalent self-declarations

### Requirement: target_name resolves via seat token

When an `LLMAction` carries a `target_name`, the resolver in `LLMAdapter` SHALL accept the canonical seat-token form `席{seat_no} {display_name}` (parsed by the `_SEAT_TOKEN_RE` pattern `^\s*席(\d+)\b`) and use the leading seat number as authoritative. A bare `display_name` without the seat-number prefix MUST resolve only when it is unambiguous across the alive seat list. An unresolvable or out-of-range token MUST cause the action to be treated as `skip` rather than be silently retargeted.

#### Scenario: Seat-token prefix is authoritative
- **WHEN** an LLM returns `target_name = "席3 アリス"` and seat 3's `display_name` is `アリス`
- **THEN** the resolver maps the action to seat 3

#### Scenario: Duplicate display_name resolves by seat-token prefix
- **WHEN** two living seats both have `display_name = "アリス"` (one human, one persona) and an LLM returns `target_name = "席5 アリス"`
- **THEN** the resolver maps the action to seat 5, not seat 3

#### Scenario: Bare display_name with collision is rejected
- **WHEN** two living seats both have `display_name = "アリス"` and an LLM returns `target_name = "アリス"` without a seat-number prefix
- **THEN** the resolver does not pick a seat at random; the action is treated as `skip`

### Requirement: Utterances respect length, language, and content invariants

Every LLM-generated utterance SHALL be Japanese, between 80 and 300 characters inclusive, and SHALL NOT quote original Gnosia dialogue verbatim or contain meta-commentary about being an AI or about the input being structured data.

#### Scenario: Over-length utterance is rejected upstream
- **WHEN** an LLM produces an utterance longer than 300 characters
- **THEN** the system prompt's hard rules cause the model to self-correct on retry, and a non-conforming response is logged and dropped rather than published as `PLAYER_SPEECH`

#### Scenario: Original Gnosia dialogue is forbidden
- **WHEN** the prompt builder renders any persona's prompt
- **THEN** the rendered prompt explicitly forbids quoting original Gnosia dialogue and requires personality imitation by tone alone
