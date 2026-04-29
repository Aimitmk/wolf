# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`wolfbot` — a Discord bot that hosts synchronous 9-player Werewolf (人狼) games. 1–9 humans join via slash commands; any unfilled seats are played by xAI Grok, DeepSeek V4 Flash, or Google Gemini 3 Flash LLM personas (selected by `LLM_PROVIDER`). Python 3.11 (strict pin `>=3.11,<3.12`), uv-managed, async-first (discord.py + aiosqlite + pydantic v2; openai client points at xAI or DeepSeek, while the Gemini path uses the official `google-genai` SDK against Vertex AI — selected per `LLM_PROVIDER`).

Full game spec (Japanese) lives at `prompts/IMPLEMENTATION_PROMPT.md` — consult it for roles, phase order, and event ordering rules before changing domain logic. Note: the top-level `prompts/` directory holds authoring/spec docs for humans and Claude (not loaded at runtime); the runtime LLM template is a separate file at `src/wolfbot/prompts/llm_system_prompt.md`, composed dynamically by `llm/prompt_builder.py` — see the LLM integration section.

Contributor-facing conventions (commit style, test naming, PR expectations) live separately in `AGENTS.md`; this file focuses on architecture and repo-specific gotchas.

## Commands

```bash
uv sync                                                  # install deps (main + dev)
uv run wolfbot                                           # run the bot (requires .env)
uv run wolfbot-npc                                       # run one NPC bot (requires NPC env vars)

uv run pytest tests                                      # full test suite
uv run pytest tests/test_rules_votes.py                  # single file
uv run pytest tests/test_rules_votes.py::test_name -v    # single test

uv run ruff check src tests                              # lint
uv run ruff format src tests                             # auto-format
uv run mypy                                              # strict typecheck (packages = ["wolfbot"])
```

- `pytest` runs with `asyncio_mode = "auto"` — do **not** decorate new async tests with `@pytest.mark.asyncio`.
- `mypy` is `strict = true` and excludes `tests/.*`. If a new untyped third-party dep is added, extend the `[[tool.mypy.overrides]]` block in `pyproject.toml` with `ignore_missing_imports = true`.
- No Makefile, pre-commit hook, or CI config exists — all QA is run manually via the commands above.
- **macOS editable-install gotcha**: `uv` sets the BSD `UF_HIDDEN` flag on `.venv/lib/.../_editable_impl_wolfbot.pth` (sometimes on every sync, including no-op audits), and Python 3.11.9+ `site.py` silently skips `.pth` files with that flag. The editable install then drops out of `sys.path` and `uv run wolfbot` dies with `ModuleNotFoundError: No module named 'wolfbot'`. **Fix committed to the venv**: `.venv/lib/python3.11/site-packages/sitecustomize.py` inserts `src/` into `sys.path` via normal module import, which is not subject to the `UF_HIDDEN` filter. If `.venv` is ever rebuilt from scratch (`rm -rf .venv && uv sync`), recreate that file with the same content, or one-shot unblock with `chflags -R nohidden .venv` (may need two passes). `pytest` and `mypy` both set their own `pythonpath` / `mypy_path`, so they pass even when the editable install is broken — don't rely on them to catch this.

## Required environment variables

Two env files, one per process. The Gameplay LLM (Master) and the NPC speech LLM (each NPC worker) are configured by **the same provider switch**, just under different env-var prefixes (`GAMEPLAY_LLM_*` vs `NPC_LLM_*`). Both reuse :class:`wolfbot.llm.decider_config.LLMDeciderConfig` internally and dispatch to one of three providers (`xai` / `deepseek` / `gemini`). The provider field is conditionally required by the role's `*_PROVIDER`; the cross-field check is a `model_validator(mode='after')` on each Settings class and fails fast at boot. Note: `*_VERTEX_PROJECT` only identifies the GCP project — Vertex AI credentials come from ADC (gcloud locally, attached service account in production), not from this var. Vertex AI Express mode and API-key auth are deliberately unsupported.

**Master** (`.env.master`, see `.env.master.example`) — loaded by `src/wolfbot/config.py::MasterSettings`, instantiated once in `main.py`:

- `DISCORD_TOKEN` — Master bot token (SecretStr)
- `DISCORD_GUILD_ID`, `MAIN_TEXT_CHANNEL_ID`, `MAIN_VOICE_CHANNEL_ID` — ints
- `WOLFBOT_DB_PATH` — SQLite path (default `./wolfbot.db`)
- `LOG_LEVEL` — default `INFO`
- `LLM_DISCUSSION_MODE` — `rounds` (default) or `reactive_voice`
- `MASTER_WS_LISTEN` — Master WS bind address (default `127.0.0.1:8800`)
- `MASTER_NPC_PSK` — optional PSK for NPC/voice-ingest WS auth (SecretStr)
- **Gameplay LLM** — drives every gameplay decision Master makes on behalf of LLM seats: votes (always), night actions (always — wolf attack / divine / guard), and day-discussion text **in rounds mode** (in reactive_voice mode the discussion text is offloaded to NPC bots, but votes / night actions still go through this LLM). Provider switch:
  - `GAMEPLAY_LLM_PROVIDER` — `xai` (default) / `deepseek` / `gemini`. Lowercase only.
  - `GAMEPLAY_LLM_API_KEY` (SecretStr) — required when provider is `xai` or `deepseek`. Any OpenAI Chat Completions–compatible endpoint works (xAI Grok / OpenAI / Groq / Together / vLLM / Ollama / DeepSeek).
  - `GAMEPLAY_LLM_MODEL` — model name (default `grok-4-1-fast`).
  - `GAMEPLAY_LLM_BASE_URL` — override the provider default base URL when pointing at a self-hosted OpenAI-compatible endpoint. Optional.
  - `GAMEPLAY_LLM_THINKING` — `enabled` (default) or `disabled`. **DeepSeek-only**, ignored otherwise.
  - `GAMEPLAY_LLM_REASONING_EFFORT` — `high` or `max` (default). **DeepSeek-only**, only forwarded when thinking is enabled.
  - `GAMEPLAY_LLM_VERTEX_PROJECT` — GCP project ID. **Required when provider is `gemini`**. Credentials come from ADC, not from this var. Empty string is rejected at boot.
  - `GAMEPLAY_LLM_VERTEX_LOCATION` — default `global`. **Gemini-only.**
  - `GAMEPLAY_LLM_THINKING_LEVEL` — `minimal` / `low` / `medium` / `high` (default `high`). **Gemini-only.**
- **Voice LLM** — separate role. The multimodal LLM that *understands human voice* — transcription + summary + CO detection + vote target extraction in one call. Default targets Google Gemini Flash via the AI Studio REST API; needed only in reactive_voice mode when voice-ingest is active.
  - `VOICE_LLM_API_KEY` — required when `MASTER_NPC_PSK` is set and you want voice-ingest active. (SecretStr)
  - `VOICE_LLM_MODEL` — default `gemini-2.0-flash-lite`.

**NPC bot** (`envs/npc/.env.<persona>`, one file per persona — see committed `envs/npc/.env.<persona>.example` templates plus [envs/npc/README.md](envs/npc/README.md)) — loaded by `src/wolfbot/npc/config.py::NpcSettings`, instantiated once per worker in `wolfbot.npc.main`. **Each NPC bot process is bound to exactly one persona at startup** (`NPC_PERSONA_KEY`); NPC bots are not interchangeable. Required fields:

- `NPC_ID` — unique NPC identifier on the WS (e.g. `npc_setsu`)
- `NPC_DISCORD_TOKEN` — this persona's Discord bot token (each persona has its own bot app, manually invited to the guild once)
- `NPC_PERSONA_KEY` — must be a key from `wolfbot.npc.personas.NPC_PERSONAS_BY_KEY` (`setsu`, `gina`, `sq`, `raqio`, `stella`, `shigemichi`, `chipie`, `comet`, `jonas`, `kukrushka`, `otome`, `sha_ming`, `remnan`, `yuriko`)
- `MASTER_WS_URL` — e.g. `ws://127.0.0.1:8800`
- `MASTER_NPC_PSK` — must match Master's value (SecretStr)
- **NPC LLM** — drives this NPC bot's short reactive utterances during DAY_DISCUSSION in reactive_voice mode. Does **not** decide votes or night actions — Master's `GAMEPLAY_LLM_*` handles those. The provider switch is symmetrical to `GAMEPLAY_LLM_*`:
  - `NPC_LLM_PROVIDER` — `xai` (default) / `deepseek` / `gemini`.
  - `NPC_LLM_API_KEY`, `NPC_LLM_MODEL`, `NPC_LLM_BASE_URL` — same semantics as the Gameplay equivalents.
  - `NPC_LLM_THINKING`, `NPC_LLM_REASONING_EFFORT` — DeepSeek-only.
  - `NPC_LLM_VERTEX_PROJECT`, `NPC_LLM_VERTEX_LOCATION`, `NPC_LLM_THINKING_LEVEL` — Gemini-only.
  - The same credential may be reused across personas, and may also be shared with Master's `GAMEPLAY_LLM_*`, but the two roles are intentionally split so each can target a different provider / model.
- `TTS_VOICE_ID`, `VOICEVOX_URL` — VOICEVOX speaker / engine
- `MAIN_VOICE_CHANNEL_ID`, `DISCORD_GUILD_ID` — must match Master
- `HEARTBEAT_INTERVAL_S`, `LOG_LEVEL`

Each NPC process picks its env file via the `WOLFBOT_NPC_ENV` env var (default: `.env.npc` in CWD, but with the `envs/npc/` layout you should always set this explicitly). Typical multi-persona deployment: `WOLFBOT_NPC_ENV=envs/npc/.env.setsu uv run wolfbot-npc` etc.

Adding a new env var = adding a typed field to the appropriate `*Settings` class — do not parse `os.environ` directly from code paths.

## Architecture

### Layering

Outer layers depend on inner; never the reverse.

```
domain/        pure: enums, frozen models, rules, state_machine  — no I/O, no asyncio
  ↑
services/      orchestration: GameService, GameEngine, RecoveryService, PermissionManager,
               LLMAdapter, DiscussionService, DiscordBotAdapter, ...
  ↑
persistence/   SqliteRepo (aiosqlite)
llm/           personas + prompt builder
prompts/       system prompt markdown (`llm_system_prompt.md`), read at runtime by prompt_builder
ui/            discord.ui Views (DM vote/action selects)
master/        Master-side reactive_voice pipeline (loaded only when MASTER_NPC_PSK is set):
               ws_server, ingest_service, logic_service, speak_arbiter, npc_registry,
               voice_ingest_service, voice_ingest_client, audio_sink, stt_service.
npc/           NPC bot worker (separate process via `uv run wolfbot-npc`):
               main, config (NpcSettings), client, speech_service,
               openai_compatible_generator, tts, playback.
main.py        Master wiring (entrypoint `wolfbot`)
```

`master/` and `npc/` are intentionally split because the two processes have disjoint runtime concerns: `master/` runs alongside the core `services/` in the Master Discord bot, while `npc/` is one-process-per-NPC and only talks to Master over WS. Anything imported by both lives in `domain/` (e.g. `domain/ws_messages.py`).

Within `services/`: `discord_service.py` contains both `DiscordBotAdapter` and the `WolfCog` slash-command dispatcher — new slash commands go in the cog, new Discord side-effects in the adapter. `submission_snapshot.py` is the shared pending-submission calculator, reused by `GameService` (early-wake checks) and `RecoveryService` (DM restoration on restart); reuse it rather than re-implement the "who still owes a submission" logic.

### The advance loop is the heart of the system

1. Each active game has a `GameEngine` (`src/wolfbot/services/timer_service.py`) watching `deadline_epoch`.
2. On deadline OR early `.wake()` (e.g. all submissions received, or host runs `/wolf force-skip`), the engine calls `GameService.advance(game_id)`.
3. `advance()` invokes a pure `src/wolfbot/domain/state_machine.plan_*()` function that returns an immutable `Transition`.
4. `GameService` applies the Transition in this order: Discord permission sync → public log announcements → DM submissions → `SqliteRepo.apply_transition(game_id, transition, expected_phase=...)`.
5. `apply_transition` is **optimistically locked** on `expected_phase`. A mismatch means a concurrent advance already happened — the call fails (logged, retried). Do not bypass the check.

Transient phases (`SETUP`, `NIGHT_0`) have `deadline_epoch=None` and auto-advance without sleeping. All other phases sleep until deadline or early wake. Phase duration constants live in the state machine / rules modules.

**LLM submissions inside `_dispatch_submissions` are fire-and-forget.** `LLMAdapter.submit_llm_votes` / `submit_llm_night_actions` / `submit_llm_discussion_rounds` / `submit_llm_runoff_candidate_speeches` (`src/wolfbot/services/llm_service.py`) schedule one `asyncio.create_task` per LLM actor and return immediately — the `await` at the call site awaits only the scheduling, not the xAI round-trip. A slow xAI response must never block `GameEngine`'s deadline watcher. Each background task re-loads the game and re-checks `phase`, `day_number`, and `ended_at` before every per-player submission, so a force-skip or deadline advance mid-flight is safely dropped. Do **not** add timeouts or awaits expecting LLM results at the `advance()` call site.

The batch tasks use two-level concurrency: a single outer `asyncio.create_task` wraps each batch (fire-and-forget from `advance()`'s perspective), and **inside** that task per-seat work runs concurrently via `asyncio.gather` — so all LLM seats hit xAI in parallel rather than serially. Wolf night-chat coordination deliberately stays serial (shared wolves-channel context, later wolves read earlier wolves' messages); preserve that distinction if you add a new submission type.

Day flow: `DAY_DISCUSSION` (two discussion rounds for LLM seats) → `DAY_VOTE` → if a tie that includes LLM candidates: `DAY_RUNOFF_SPEECH` (one speech per tied LLM candidate) → `DAY_RUNOFF` → `NIGHT`. Per-LLM-seat speech progress is tracked in the `llm_speech_counts` table (`discussion_rounds_done`, `runoff_speech_done`) so a force-skip / restart mid-flight resumes correctly without double-posting. `DAY_VOTE` and `DAY_RUNOFF` advances are additionally gated by `GameService._vote_resolution_due()` — votes resolve only when the deadline is reached, all votes are in, or the host force-skipped. A bare wake before the deadline is dropped, not parked in `WAITING_HOST_DECISION`.

### Circular dependency resolution

See `src/wolfbot/main.py` lines ~44–56. `DiscordBotAdapter` and `LLMAdapter` are constructed **before** `GameService`, then `set_game_service(...)` injects the back-reference once `GameService` exists. Preserve this pattern when adding a new adapter that needs to call back into `GameService`.

### Protocols for testability

`GameService` talks to its collaborators through Protocols defined alongside it (`DiscordAdapter`, `LLMAdapter`, `LLMActionDecider`, `MessagePoster`, `WakeSink`). `RecoveryService` defines its own narrower `RecoveryDiscordAdapter` for startup reconciliation. Tests swap in `FakeDiscordAdapter`, `FakeLLMAdapter`, `FakeClock` from `tests/fakes.py`. When you add a new collaborator, define a Protocol and a Fake — do **not** reach into `discord.py` or the xAI client from tests.

### Domain model split

- **Frozen Pydantic models** (`ConfigDict(frozen=True)`) for data that must not mutate mid-flight: `Seat`, `LogEntry`, `PendingDecision`, `Transition`, `VoteOutcome`.
- **Mutable live state** rehydrated from DB per operation: `Player`, `Game`, `Vote`, `NightAction`.
- Atomic replacement is via `apply_transition`. Never mutate a model after it's been committed.
- `submit_vote` / `submit_night_action` return `SubmitResult` (`src/wolfbot/domain/enums.py`) — a StrEnum of specific rejection reasons the UI surfaces back to the player. New submission endpoints should return `SubmitResult`, not a bool.
- Both submission endpoints require a `day: int` argument that must match `game.day_number`; otherwise they return `SubmitResult.STALE_PHASE`. `VoteView` / `NightActionView` capture the current `day` at DM-send time so a player clicking yesterday's DM today is rejected even when the phase happens to match.
- `PendingSubmission` has two parallel seat lists: `missing_seats` (never submitted) and `unresolved_seats` (submitted but unsettled — currently only wolf attack splits). `resend_pending_dms` re-sends DMs to the union of both, so `/wolf extend` can break a split lockout without needing `/wolf force-skip`.
- `DiscordAdapter.send_night_action_dms(game, actors, alive_players, seats)` intentionally takes two separate player pools: `actors` = who to DM (typically the pending subset), `alive_players` = the full alive pool used to compute legal targets. Keep them separate in any new code path — a resend to a single not-yet-submitted wolf during a split still needs to offer the full legal attack list.
- Lobby seat mutations must go through `SqliteRepo.join_lobby` / `leave_lobby` (phase-guarded in one tx). Do **not** call raw `insert_seat` / `delete_seat` from command paths — those exist for test setup. This keeps stale `/wolf join` / `/wolf leave` from corrupting a game that already transitioned out of LOBBY.
- The `LOBBY → SETUP` transition plus LLM-seat backfill goes through `SqliteRepo.claim_start_and_backfill`, which packages the phase flip and the bot-seat inserts into one optimistically-locked transaction (matches on `expected_phase=LOBBY`). `/wolf start` uses this; don't re-implement the flow with a separate phase update followed by `insert_seat` calls — a concurrent `/wolf join` / `/wolf leave` would slip in between.
- `force_skip_pending` is set only via `Transition.set_force_skip=True` passed to `apply_transition`. That way the flag flip and the `WAITING_HOST_DECISION → paused phase` swap share a transaction — if `/wolf extend` wins the race, both roll back together. There is no standalone `repo.set_force_skip` method.
- `GameService.host_abort` returns `bool` — `False` means the game was already ended and no work was done. The `/wolf abort` handler in `discord_service.py` branches on this: only on `True` does it detach + stop the `GameEngine` and post the public "強制終了" message; otherwise it replies ephemerally. New callers of `host_abort` must respect the same pattern or risk double-teardown.

### Role reveals & detection semantics

- Seer divination and medium post-mortem return **bool, not `Faction`**, via `domain/rules.py::is_detected_as_wolf(role)`. Madman is **not** detected as wolf (same result as villager). When adding a new role, decide whether it feeds this predicate rather than branching on `Role` directly in callers — the seer/medium UI copy assumes a binary.
- At game end, `domain/state_machine.py::_role_reveal_log` appends a single `ROLE_REVEAL`-kind `LogEntry` listing every seat's final role + alive/dead status. It is emitted from **both** win paths — execution victory (~line 512) and attack victory (~line 744). Any new end-of-game transition must emit this log, or the public reveal will be missing.

### Recovery on startup

`src/wolfbot/services/recovery_service.py` iterates all games with `ended_at IS NULL`:

- If `deadline_epoch < now`, the game is parked in `WAITING_HOST_DECISION`. The host must intervene via `/wolf force-skip` or `/wolf extend`. This is deliberate — do **not** auto-resolve stale actions silently.
- Otherwise, reconcile Discord permissions and reattach a `GameEngine`.
- Per-game isolation: one game's failed recovery must not block others.

### Persistence schema

`src/wolfbot/persistence/schema.py` is idempotent DDL (`CREATE TABLE/INDEX IF NOT EXISTS`), applied on every boot via `migrate()`. There is no alembic, no version table. Adding a column means editing `schema.py` with a nullable or defaulted column so existing DBs upgrade cleanly on the next boot — destructive migrations (drops, renames, type changes) have no first-class support and require a manual plan. The `llm_speech_counts` table illustrates the additive-migration pattern: `CREATE TABLE IF NOT EXISTS` for the base shape, plus per-column `ALTER TABLE ADD COLUMN` blocks guarded by `PRAGMA table_info` checks so re-runs are no-ops.

### Discord channel permissions

`src/wolfbot/services/permission_manager.py` reconciles three channel classes:

- **Main text** — all living players see+send; dead players see only.
- **Wolves** (private) — living werewolves see; send only during `NIGHT`.
- **Heaven** (private) — dead players see+send; living players cannot see.

The manager is idempotent: it only issues API calls on actual diffs. Don't send blind permission updates from other code paths.

On game end, `heaven_channel_id` / `wolves_channel_id` are **deleted** (not just permission-cleared) — a deliberate fix for cross-game channel leak. Preserve this on any future game-teardown path.

During wolf-attack splits, the main channel announces only `未確定: N件` (hiding the exact target breakdown), while the wolves-private channel sees the real split tally. This asymmetric disclosure is intentional — do not "simplify" by posting the split detail to both channels.

### LLM integration

`src/wolfbot/services/llm_service.py` exposes three deciders selected by `make_llm_decider(cfg)` where `cfg: LLMDeciderConfig` is built from `MasterSettings.gameplay_decider_config()` (or `NpcSettings.npc_decider_config()` on the NPC side). The provider switch is one shared abstraction (`wolfbot.llm.decider_config.LLMDeciderConfig`) used by both the gameplay decider factory and the NPC speech generator factory (`wolfbot.npc.generator_factory.make_npc_generator`). The Master env prefix is `GAMEPLAY_LLM_*`; the NPC env prefix is `NPC_LLM_*`; the field semantics (provider, api_key, model, base_url, thinking, reasoning_effort, vertex_project, vertex_location, thinking_level) are identical.

`XAILLMActionDecider` calls the configured OpenAI-compatible endpoint (default `https://api.x.ai/v1/chat/completions`; override via `*_LLM_BASE_URL` to point at OpenAI / Groq / Together / vLLM / Ollama / etc.) with `response_format={"type":"json_schema", "json_schema": RESPONSE_SCHEMA}` strict mode. Grok rejects `reasoning_effort`/`extra_body`, so the xAI path deliberately sends neither. `DeepSeekLLMActionDecider` calls `https://api.deepseek.com` with `response_format={"type":"json_object"}` (DeepSeek doesn't support strict json_schema) plus a per-call JSON contract appended to the system prompt by `_deepseek_json_contract` so the model knows the exact field names; `*_LLM_THINKING` toggles `extra_body={"thinking": {"type": ...}}` and, when enabled, forwards `reasoning_effort` (`high`/`max`). DeepSeek's `reasoning_content` is intentionally never read, logged, or persisted — only `message.content` is consumed. `GeminiLLMActionDecider` calls Vertex AI's Gemini API via the official `google-genai` SDK (`genai.Client(vertexai=True, project=..., location=...)` — endpoint is in the `aiplatform.googleapis.com` family, resolved by the SDK; with `location="global"` the SDK targets `https://aiplatform.googleapis.com/`). Authentication is ADC/IAM only (no API key); locally `gcloud auth application-default login`, in production an attached service account with Vertex AI permissions. Vertex AI Express mode and API-key auth are deliberately unsupported. Request shape: `client.aio.models.generate_content(...)` with `response_mime_type="application/json"` + `response_json_schema=RESPONSE_SCHEMA["schema"]` (Gemini 3 structured outputs), plus `thinking_config=types.ThinkingConfig(thinking_level=...)`; default `thinking_level="high"`. Gemini's internal thinking / thought signatures are never read, logged, or persisted — only `resp.text` is consumed (parallel to DeepSeek's `reasoning_content` rule). All three paths funnel through `LLMAction.model_validate_json` and share the same `tenacity` retry policy. The runtime markdown template (`src/wolfbot/prompts/llm_system_prompt.md`) is unchanged — the DeepSeek JSON contract is added at decider time only on the DeepSeek path; Gemini relies on `response_json_schema` and xAI on `json_schema` strict mode.

The NPC speech path mirrors this exactly: `OpenAICompatibleNpcGenerator` handles xAI / OpenAI / DeepSeek (with `mode="json_object"`) by reusing the same decider-config-style switch in its config dataclass; `GeminiNpcGenerator` handles Vertex AI. The factory `wolfbot.npc.generator_factory.make_npc_generator(cfg, persona_key)` picks the right one based on `cfg.provider` and binds the persona to the worker process.

### Reactive voice pipeline (realtime chat)

When `LLM_DISCUSSION_MODE=reactive_voice` and `MASTER_NPC_PSK` is set, `main.py` wires a realtime voice pipeline:

- **Master side** (`main.py`): `InMemoryNpcRegistry` + `SpeakArbiter` + `MasterIngestService` + `WebsocketsMasterWsServer`. The Master joins VC via `voice_recv.VoiceRecvClient` + `WolfbotAudioSink` for STT ingest, receives human speech via `GeminiAudioAnalyzer`, and dispatches NPC turns via the arbiter. On phase enter, `_on_reactive_phase_enter` assigns online NPCs to LLM seats.
- **NPC side** (`wolfbot.npc.main`, one process per NPC bot, **bound to one persona at startup**): `discord.Client` joins VC → WebSocket to Master (PSK auth, register payload carries `persona_key`) → `NpcClient` handles `SpeakRequest` → `OpenAICompatibleNpcGenerator` (NPC LLM call, structured JSON; default targets xAI Grok) renders speech in the bound persona → `NpcSpeechService` → `VoicevoxTtsService` (local VOICEVOX, 24kHz WAV) → `DiscordVoicePlayback` (`discord.FFmpegPCMAudio`). All NPC-side modules live under `wolfbot.npc.*`; the Master-side counterpart (`InMemoryNpcRegistry`, `SpeakArbiter`, `MasterIngestService`, `WebsocketsMasterWsServer`) lives under `wolfbot.master.*`. The NPC LLM backend is swappable via `NPC_LLM_BASE_URL` + `NPC_LLM_MODEL` in the persona's env file — no code change needed to point at OpenAI / Groq / Together / vLLM / Ollama / etc.

- **Persona binding model**: each NPC bot embodies one persona forever (declared via `NPC_PERSONA_KEY` in its env file, validated against `NPC_PERSONAS_BY_KEY` at startup). On `npc_register` the NPC bot tells Master its `persona_key`; Master stores it on `NpcEntry`. When `/wolf start` runs in `reactive_voice` mode, `WolfCog._select_llm_seat_personas` pulls online NPC bots from the registry (skipping any already assigned to another active game) and uses their personas as the LLM seats — there is no separate "Master rolls personas" step. If fewer NPC bots are online than the LLM-seat shortfall, `/wolf start` fails with a friendly message. In `rounds` mode (no NPC bot processes), the same path falls through to a random `pick_personas(NPC_PERSONAS, ...)` draw because Master drives those LLM seats internally without VC playback.
- **Seat assignment**: `_on_reactive_phase_enter` in `main.py` pairs online NPC bots with unassigned LLM seats via `npc_registry.assign()`. The arbiter only dispatches to NPCs with an assigned seat.
- **Entry point**: `WOLFBOT_NPC_ENV=envs/npc/.env.<persona> uv run wolfbot-npc` (each persona has its own committed `envs/npc/.env.<persona>.example` template; copy it to `envs/npc/.env.<persona>`, fill in the secrets — `NPC_DISCORD_TOKEN`, `MASTER_NPC_PSK`, `NPC_LLM_API_KEY`, `DISCORD_GUILD_ID`, `MAIN_VOICE_CHANNEL_ID` — and start one process per persona). See [envs/npc/README.md](envs/npc/README.md) for the persona / TTS_VOICE_ID table.

The system prompt is **composed per actor** by `src/wolfbot/llm/prompt_builder.py`, not loaded verbatim. Three programmatically-generated blocks are layered onto `src/wolfbot/prompts/llm_system_prompt.md`: `_build_game_rules_block()` (9-player ruleset derived from `ROLE_DISTRIBUTION` + `VILLAGE_SIZE` so the canonical numbers aren't duplicated, plus the shared reasoning heuristics every seat sees — currently CO evaluation: a **never-countered** single role-CO is presumed near-real, but a **sole survivor** of a contested CO history (same role had ≥2 COs, others died) is **not** auto-trusted; topical mention of a CO ("the seer CO 〜について") is distinguished from self-declaration; counter-COs and divination/attack alignment feed the judgment), `_ROLE_STRATEGIES[role]` (role-scoped tactical hints — wolf/madman carry day-phased fake-CO playbooks that deliberately mirror each other, knight carries peaceful-morning guard-CO guidance, seer/medium/villager carry judgment-integrity rules (villager strategy explicitly forbids 「村人CO」/「素村CO」 and equivalents); cross-leak tests assert one role never sees another's strategy), and `_build_speech_profile_block(persona)` (the persona's 話法 section). Routing when editing: shared heuristics every seat should see → `_build_game_rules_block`; role-specific strategy → `_ROLE_STRATEGIES`; base framing / output format / hard invariants → the markdown template. The markdown is a template, not the whole prompt.

The legacy `context_analysis` CO parser was removed (commit `b29c4f7`); LLM seats now read raw public-log `PLAYER_SPEECH` entries and apply the CO-detection rules from the system prompt themselves. Don't reintroduce a pre-digested CO summary.

Personas in `src/wolfbot/llm/personas.py` are **Gnosia-flavored archetypes** with two parallel fields, kept semantically separate:

- `style_guide` — free-form prose: judgment tendency, stance, tone register (判断/トーン).
- `speech_profile: SpeechProfile` (frozen dataclass) — structured speech reproduction (喋り方/語彙/文体): `first_person`, `self_reference_aliases`, `address_style`, `sentence_style`, `pause_style`, `signature_phrases`, `forbidden_overuse`, `narration_mode`. Kukrushka alone uses `narration_mode="silent_gesture"` — her block renders gesture descriptions instead of a normal speech profile. Do not bleed speech data into `style_guide` or vice versa.

Hard rules enforced by the system prompt (`src/wolfbot/prompts/llm_system_prompt.md`):

- Never quote original Gnosia dialogue; imitate personality via tone only.
- No meta-commentary (no "as an AI", no referring to inputs as data).
- Japanese only, 80–300 chars per utterance.
- `target_name` must exactly match a candidate **token** (`席{seat_no} {display_name}`) or be `null` / intent=`skip`. The seat-number prefix disambiguates duplicate display_names (e.g. two humans named "Alice", or a human colliding with a persona). `LLMAdapter._resolve_target` parses the prefix; bare display_names still resolve when unambiguous (legacy fallback).
- Persona `display_name` is a katakana handle prefixed with a distinguishing emoji (e.g. `🌙 セツ`). The emoji is part of the stored `display_name` string — `seat_token` includes it verbatim and the target resolver handles it transparently. When adding a persona, pick an emoji not already used by another persona.

## Testing conventions

- `tests/conftest.py` provides `frozen_rng` (seed 42, deterministic role shuffles), `seats` (canonical 9-seat lineup), and async `repo` (tempfile-backed `SqliteRepo` with schema already migrated).
- For engine/timing tests use `FakeClock` from `tests/fakes.py` — do **not** mock `time.time` or `asyncio.sleep` directly.
- Per-file test lint relaxations (`B011`, `RUF001-003`) are already configured in `pyproject.toml`; no need to sprinkle `noqa`.

## Gotchas

- Python is pinned `>=3.11,<3.12`. Do not introduce 3.12+ syntax (e.g. the `type` statement).
- Ruff `E501` is ignored — the formatter handles line length. Don't hand-wrap at 100.
- SQLite foreign keys are `ON DELETE CASCADE`: deleting a `games` row cascades to seats/votes/logs.
