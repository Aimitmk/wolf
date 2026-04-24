# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`wolfbot` — a Discord bot that hosts synchronous 9-player Werewolf (人狼) games. 1–9 humans join via slash commands; any unfilled seats are played by xAI Grok LLM personas. Python 3.11 (strict pin `>=3.11,<3.12`), uv-managed, async-first (discord.py + aiosqlite + pydantic v2 + openai client pointed at the xAI endpoint).

Full game spec (Japanese) lives at `prompts/IMPLEMENTATION_PROMPT.md` — consult it for roles, phase order, and event ordering rules before changing domain logic. Note: the top-level `prompts/` directory holds authoring/spec docs for humans and Claude (not loaded at runtime); the runtime LLM template is a separate file at `src/wolfbot/prompts/llm_system_prompt.md`, composed dynamically by `llm/prompt_builder.py` — see the LLM integration section.

Contributor-facing conventions (commit style, test naming, PR expectations) live separately in `AGENTS.md`; this file focuses on architecture and repo-specific gotchas.

## Commands

```bash
uv sync                                                  # install deps (main + dev)
uv run wolfbot                                           # run the bot (requires .env)

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

From `.env.example`. All must be set for the bot to start:

- `DISCORD_TOKEN` — bot token (SecretStr)
- `XAI_API_KEY` — xAI API key (SecretStr)
- `XAI_MODEL` — model name (default `grok-4-1-fast`)
- `DISCORD_GUILD_ID`, `MAIN_TEXT_CHANNEL_ID`, `MAIN_VOICE_CHANNEL_ID` — ints
- `WOLFBOT_DB_PATH` — SQLite path (default `./wolfbot.db`)
- `LOG_LEVEL` — default `INFO`

Loaded at boot by `src/wolfbot/config.py::Settings` (pydantic-settings, reads `.env`, instantiated once in `main.py`). Adding a new env var = adding a typed field to `Settings` — do not parse `os.environ` directly from code paths.

## Architecture

### Layering

Outer layers depend on inner; never the reverse.

```
domain/        pure: enums, frozen models, rules, state_machine  — no I/O, no asyncio
  ↑
services/      orchestration: GameService, GameEngine, RecoveryService, PermissionManager
  ↑
persistence/   SqliteRepo (aiosqlite)
llm/           personas + prompt builder
prompts/       system prompt markdown (`llm_system_prompt.md`), read at runtime by prompt_builder
ui/            discord.ui Views (DM vote/action selects)
main.py        wiring
```

Within `services/`: `discord_service.py` contains both `DiscordBotAdapter` and the `WolfCog` slash-command dispatcher — new slash commands go in the cog, new Discord side-effects in the adapter. `submission_snapshot.py` is the shared pending-submission calculator, reused by `GameService` (early-wake checks) and `RecoveryService` (DM restoration on restart); reuse it rather than re-implement the "who still owes a submission" logic.

### The advance loop is the heart of the system

1. Each active game has a `GameEngine` (`src/wolfbot/services/timer_service.py`) watching `deadline_epoch`.
2. On deadline OR early `.wake()` (e.g. all submissions received, or host runs `/wolf force-skip`), the engine calls `GameService.advance(game_id)`.
3. `advance()` invokes a pure `src/wolfbot/domain/state_machine.plan_*()` function that returns an immutable `Transition`.
4. `GameService` applies the Transition in this order: Discord permission sync → public log announcements → DM submissions → `SqliteRepo.apply_transition(game_id, transition, expected_phase=...)`.
5. `apply_transition` is **optimistically locked** on `expected_phase`. A mismatch means a concurrent advance already happened — the call fails (logged, retried). Do not bypass the check.

Transient phases (`SETUP`, `NIGHT_0`) have `deadline_epoch=None` and auto-advance without sleeping. All other phases sleep until deadline or early wake. Phase duration constants live in the state machine / rules modules.

**LLM submissions inside `_dispatch_submissions` are fire-and-forget.** `LLMAdapter.submit_llm_votes` / `submit_llm_night_actions` / `submit_llm_daystart_speeches` (`src/wolfbot/services/llm_service.py`) schedule one `asyncio.create_task` per LLM actor and return immediately — the `await` at the call site awaits only the scheduling, not the xAI round-trip. A slow xAI response must never block `GameEngine`'s deadline watcher. Each background task re-loads the game and re-checks `phase`, `day_number`, and `ended_at` before every per-player submission, so a force-skip or deadline advance mid-flight is safely dropped. Do **not** add timeouts or awaits expecting LLM results at the `advance()` call site.

The batch tasks use two-level concurrency: a single outer `asyncio.create_task` wraps each batch (fire-and-forget from `advance()`'s perspective), and **inside** that task per-seat work runs concurrently via `asyncio.gather` — so all LLM seats hit xAI in parallel rather than serially. Wolf night-chat coordination deliberately stays serial (shared wolves-channel context, later wolves read earlier wolves' messages); preserve that distinction if you add a new submission type.

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

`src/wolfbot/persistence/schema.py` is idempotent DDL (`CREATE TABLE/INDEX IF NOT EXISTS`), applied on every boot via `migrate()`. There is no alembic, no version table. Adding a column means editing `schema.py` with a nullable or defaulted column so existing DBs upgrade cleanly on the next boot — destructive migrations (drops, renames, type changes) have no first-class support and require a manual plan.

### Discord channel permissions

`src/wolfbot/services/permission_manager.py` reconciles three channel classes:

- **Main text** — all living players see+send; dead players see only.
- **Wolves** (private) — living werewolves see; send only during `NIGHT`.
- **Heaven** (private) — dead players see+send; living players cannot see.

The manager is idempotent: it only issues API calls on actual diffs. Don't send blind permission updates from other code paths.

On game end, `heaven_channel_id` / `wolves_channel_id` are **deleted** (not just permission-cleared) — a deliberate fix for cross-game channel leak. Preserve this on any future game-teardown path.

During wolf-attack splits, the main channel announces only `未確定: N件` (hiding the exact target breakdown), while the wolves-private channel sees the real split tally. This asymmetric disclosure is intentional — do not "simplify" by posting the split detail to both channels.

### LLM integration

`src/wolfbot/services/llm_service.py` uses the `openai` client pointed at `https://api.x.ai/v1/chat/completions`. `response_format` enforces the `LLMAction` JSON schema strictly, and `tenacity` retries on transient errors.

The system prompt is **composed per actor** by `src/wolfbot/llm/prompt_builder.py`, not loaded verbatim. Three programmatically-generated blocks are layered onto `src/wolfbot/prompts/llm_system_prompt.md`: `_build_game_rules_block()` (9-player ruleset derived from `ROLE_DISTRIBUTION` + `VILLAGE_SIZE` so the canonical numbers aren't duplicated, plus the shared reasoning heuristics every seat sees — currently CO evaluation: a single CO is presumed near-real unless contradicted, counter-COs are judged by timestamp / vote consistency / divination / attack alignment), `_ROLE_STRATEGIES[role]` (role-scoped tactical hints — wolf/madman carry day-phased fake-CO playbooks that deliberately mirror each other, knight carries peaceful-morning guard-CO guidance, seer/medium/villager carry judgment-integrity rules; cross-leak tests assert one role never sees another's strategy), and `_build_speech_profile_block(persona)` (the persona's 話法 section). Routing when editing: shared heuristics every seat should see → `_build_game_rules_block`; role-specific strategy → `_ROLE_STRATEGIES`; base framing / output format / hard invariants → the markdown template. The markdown is a template, not the whole prompt.

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
