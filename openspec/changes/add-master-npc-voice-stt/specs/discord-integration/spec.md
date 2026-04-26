## MODIFIED Requirements

### Requirement: /wolf slash commands are registered to a single guild

The Master bot SHALL register the `/wolf` command set (`join`, `leave`, `start`, `abort`, `force-skip`, `extend`) inside a single Discord guild whose id is read from the `DISCORD_GUILD_ID` setting. Host-only commands (`start`, `abort`, `force-skip`, `extend`) MUST verify that the invoking user is the recorded game host before performing any state mutation. **NPC bot workers and the voice-ingest worker run as separate Discord bot identities in the same guild and SHALL NOT register any slash commands** — only the Master bot owns the `/wolf` command surface. NPC bots and voice-ingest authenticate to Discord with their own tokens but do not expose any user-facing commands.

#### Scenario: Non-host abort attempt is rejected
- **WHEN** a user who is not the host invokes `/wolf abort` on an active game
- **THEN** the bot replies ephemerally with a permission error and does not call `GameService.host_abort`

#### Scenario: Host abort tears down only on first call
- **WHEN** the host invokes `/wolf abort` while the game is still active
- **THEN** `GameService.host_abort` returns `True`, the cog detaches and stops the `GameEngine`, and posts the public `強制終了` message

#### Scenario: Host abort on already-ended game is a no-op
- **WHEN** the host invokes `/wolf abort` on a game whose `ended_at` is non-null
- **THEN** `GameService.host_abort` returns `False`, the cog replies ephemerally, and no public message is posted and no engine teardown runs

#### Scenario: NPC bots register no slash commands
- **WHEN** a `wolfbot-npc` process connects to Discord with `NPC_DISCORD_TOKEN`
- **THEN** that bot does not register any `/wolf` (or other) application commands; only the Master bot owns the slash-command surface for the guild

## ADDED Requirements

### Requirement: NPC bots and voice-ingest share the main voice channel

When `LLM_DISCUSSION_MODE=reactive_voice` is active for a game, every NPC bot worker AND the voice-ingest worker SHALL join the Discord voice channel identified by `MAIN_VOICE_CHANNEL_ID` (the same channel humans speak in). NPC bots act as audio sources only (TTS playback). voice-ingest acts as a listener only (no playback). There is no per-game voice channel allocation in the MVP and no separate `NPC_VOICE_CHANNEL_ID`.

#### Scenario: All NPC bots and voice-ingest join the same VC
- **WHEN** a reactive_voice game starts with three NPC seats and voice-ingest is enabled
- **THEN** the three `wolfbot-npc` processes and the voice-ingest worker all join the channel referenced by `MAIN_VOICE_CHANNEL_ID`

#### Scenario: voice-ingest does not play audio
- **WHEN** the voice-ingest worker is present in `MAIN_VOICE_CHANNEL_ID`
- **THEN** it never invokes any voice-send / playback API; it only consumes incoming audio packets

### Requirement: NPC bots play VC audio only after Master authorization

NPC bot workers MUST gate all VC audio output on having received a valid `PlaybackAuthorized` from Master for a specific `request_id`. NPC bots MUST NOT play any audio they generate independently (e.g. test playback, ambient effects, retries of a rejected utterance) in the live VC.

#### Scenario: Unauthorized playback is suppressed
- **WHEN** an NPC bot has a finished Grok response and finished TTS audio cached but has not received `PlaybackAuthorized` for that `request_id`
- **THEN** the NPC bot does not play the audio in `MAIN_VOICE_CHANNEL_ID`

#### Scenario: Authorized playback proceeds
- **WHEN** an NPC bot receives `PlaybackAuthorized` for `request_id=sr_01HX`
- **THEN** the NPC bot plays the corresponding TTS audio in `MAIN_VOICE_CHANNEL_ID` and emits `playback_finished` upon completion

### Requirement: NPC bots stay silent when no game is active for them

NPC bot workers MUST remain silent (no VC audio, no Discord text post, no DM) when no game has assigned them a seat or when the assigned game's phase is not one in which `SpeakArbiter` could legitimately request speech (e.g. `LOBBY`, `SETUP`, `NIGHT_0`, `NIGHT`, `DAY_VOTE`, `DAY_RUNOFF`, `WAITING_HOST_DECISION`, `GAME_OVER`).

#### Scenario: Idle NPC bot is silent
- **WHEN** no game is assigned to NPC `npc_p5`, or the assigned game is in `LOBBY`
- **THEN** the NPC bot makes no Discord-side action — no VC playback, no message in any channel

#### Scenario: NPC bot is silent during NIGHT
- **WHEN** the assigned game enters `NIGHT`
- **THEN** the NPC bot performs no VC playback even if it receives a stale `SpeakRequest`; the Master arbiter would in any case not issue one

### Requirement: voice-ingest excludes NPC bot identities by registry lookup

The voice-ingest worker SHALL fetch the current NPC bot `discord_bot_user_id` set from the Master NPC registry and discard any incoming voice packet whose `user_id` is in that set, before VAD or STT runs (see voice-ingest spec for full rules). This requirement is reflected in the discord-integration capability because it is the boundary at which Discord-supplied user identity becomes the authoritative filter for "is this a human".

#### Scenario: NPC TTS audio is filtered out by user_id
- **WHEN** an NPC bot plays TTS in `MAIN_VOICE_CHANNEL_ID` and Discord routes the corresponding voice packets to voice-ingest
- **THEN** voice-ingest discards those packets at the receive boundary and does not run them through VAD or STT
