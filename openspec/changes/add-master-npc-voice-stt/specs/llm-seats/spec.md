## MODIFIED Requirements

### Requirement: Utterances respect length, language, and content invariants

Every LLM-generated utterance SHALL be Japanese, SHALL NOT quote original Gnosia dialogue verbatim, and SHALL NOT contain meta-commentary about being an AI or about the input being structured data. Length is constrained per `LLM_DISCUSSION_MODE`: under `rounds`, utterances MUST be between 80 and 300 characters inclusive; under `reactive_voice`, utterances MUST be **80 characters or fewer** (and MAY be shorter, including a single short interjection of a handful of characters).

#### Scenario: Over-length utterance under rounds mode is rejected
- **WHEN** an LLM produces an utterance longer than 300 characters under `LLM_DISCUSSION_MODE=rounds`
- **THEN** the system prompt's hard rules cause the model to self-correct on retry, and a non-conforming response is logged and dropped rather than published as `PLAYER_SPEECH`

#### Scenario: Over-length utterance under reactive_voice mode is rejected
- **WHEN** an LLM produces an utterance longer than 80 characters under `LLM_DISCUSSION_MODE=reactive_voice`
- **THEN** the SpeakArbiter rejects the corresponding `SpeakResult` with `failure_reason=utterance_too_long`, no `SpeechEvent` is written, and the NPC bot receives `playback_rejected`

#### Scenario: Short reactive interjection is allowed under reactive_voice mode
- **WHEN** an LLM produces a 12-character utterance such as `「うーん…怪しいかも」` under `LLM_DISCUSSION_MODE=reactive_voice`
- **THEN** the utterance is accepted, recorded as `SpeechEvent(source=npc_generated)`, and authorized for playback

#### Scenario: Original Gnosia dialogue is forbidden in any mode
- **WHEN** the prompt builder renders any persona's prompt under any `LLM_DISCUSSION_MODE`
- **THEN** the rendered prompt explicitly forbids quoting original Gnosia dialogue and requires personality imitation by tone alone

## ADDED Requirements

### Requirement: NPC bot worker process consumes LogicPacket instead of full game snapshot

When `LLM_DISCUSSION_MODE=reactive_voice`, each NPC seat SHALL be served by a separate `wolfbot-npc` worker process which receives **only** Master-published `LogicPacket` payloads for day-discussion utterances — never the full game snapshot, never other NPCs' private state, never the master role assignments. The NPC bot SHALL combine the `LogicPacket` with its own persona, role, and private state to compose the Grok prompt. The persona registry, the prompt-builder pipeline, the role-strategy isolation rules, the structured-output `LLMAction` schema, and the seat-token resolver MUST be reused unchanged.

#### Scenario: NPC bot does not see other NPCs' private state
- **WHEN** Master sends a `LogicPacket` to NPC `npc_p5`
- **THEN** the payload contains no role assignments, no private state, no LLM-context for any seat other than `npc_p5`'s own

#### Scenario: Persona registry is shared with NPC bot processes
- **WHEN** an NPC bot worker starts with `NPC_ID=npc_p5`
- **THEN** the NPC bot loads the persona for `npc_p5` from the same `wolfbot.llm.personas.PERSONAS_BY_KEY` registry the Master process uses

#### Scenario: Prompt-builder isolation is preserved across processes
- **WHEN** an NPC bot worker for a seat whose role is `villager` composes its system prompt
- **THEN** the rendered prompt contains the villager strategy block and does NOT contain any wolf, madman, or other-role strategy text

### Requirement: rounds-mode behavior remains in-process

Under `LLM_DISCUSSION_MODE=rounds`, LLM seats SHALL continue to be served by the in-process `LLMAdapter` exactly as today: structured-output requests issued from the Master process, results consumed by the existing `submit_llm_*` flows, no separate `wolfbot-npc` worker required. The new NPC-bot worker MUST NOT be required to be running for `rounds`-mode games.

#### Scenario: rounds-mode game runs without NPC bot worker
- **WHEN** a game starts under `LLM_DISCUSSION_MODE=rounds` while no `wolfbot-npc` workers are connected
- **THEN** the game runs end-to-end: `submit_llm_discussion_rounds`, `submit_llm_votes`, `submit_llm_night_actions`, and the runoff-speech path all operate via the in-process `LLMAdapter` and produce the same observable behavior as today
