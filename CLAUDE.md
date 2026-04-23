# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`wolfbot` — a Discord bot that hosts synchronous 9-player Werewolf (人狼) games. 1–9 humans join via slash commands; any unfilled seats are played by xAI Grok LLM personas. Python 3.11 (strict pin `>=3.11,<3.12`), uv-managed, async-first (discord.py + aiosqlite + pydantic v2 + openai client pointed at the xAI endpoint).

Full game spec (Japanese) lives at `prompts/IMPLEMENTATION_PROMPT.md` — consult it for roles, phase order, and event ordering rules before changing domain logic.

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

## Required environment variables

From `.env.example`. All must be set for the bot to start:

- `DISCORD_TOKEN` — bot token (SecretStr)
- `XAI_API_KEY` — xAI API key (SecretStr)
- `XAI_MODEL` — model name (default `grok-4-1-fast`)
- `DISCORD_GUILD_ID`, `MAIN_TEXT_CHANNEL_ID`, `MAIN_VOICE_CHANNEL_ID` — ints
- `WOLFBOT_DB_PATH` — SQLite path (default `./wolfbot.db`)
- `LOG_LEVEL` — default `INFO`

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
ui/            discord.ui Views (DM vote/action selects)
main.py        wiring
```

### The advance loop is the heart of the system

1. Each active game has a `GameEngine` (`src/wolfbot/services/timer_service.py`) watching `deadline_epoch`.
2. On deadline OR early `.wake()` (e.g. all submissions received, or host runs `/wolf force-skip`), the engine calls `GameService.advance(game_id)`.
3. `advance()` invokes a pure `src/wolfbot/domain/state_machine.plan_*()` function that returns an immutable `Transition`.
4. `GameService` applies the Transition in this order: Discord permission sync → public log announcements → DM submissions → `SqliteRepo.apply_transition(game_id, transition, expected_phase=...)`.
5. `apply_transition` is **optimistically locked** on `expected_phase`. A mismatch means a concurrent advance already happened — the call fails (logged, retried). Do not bypass the check.

Transient phases (`SETUP`, `NIGHT_0`) have `deadline_epoch=None` and auto-advance without sleeping. All other phases sleep until deadline or early wake. Phase duration constants live in the state machine / rules modules.

### Circular dependency resolution

See `src/wolfbot/main.py` lines ~44–56. `DiscordBotAdapter` and `LLMAdapter` are constructed **before** `GameService`, then `set_game_service(...)` injects the back-reference once `GameService` exists. Preserve this pattern when adding a new adapter that needs to call back into `GameService`.

### Protocols for testability

`GameService` talks to its collaborators through Protocols defined alongside it (`DiscordAdapter`, `LLMAdapter`, `LLMActionDecider`, `MessagePoster`, `WakeRegistry`). Tests swap in `FakeDiscordAdapter`, `FakeLLMAdapter`, `FakeClock` from `tests/fakes.py`. When you add a new collaborator, define a Protocol and a Fake — do **not** reach into `discord.py` or the xAI client from tests.

### Domain model split

- **Frozen Pydantic models** (`ConfigDict(frozen=True)`) for data that must not mutate mid-flight: `Seat`, `LogEntry`, `PendingDecision`, `Transition`, `VoteOutcome`.
- **Mutable live state** rehydrated from DB per operation: `Player`, `Game`, `Vote`, `NightAction`.
- Atomic replacement is via `apply_transition`. Never mutate a model after it's been committed.

### Recovery on startup

`src/wolfbot/services/recovery_service.py` iterates all games with `ended_at IS NULL`:

- If `deadline_epoch < now`, the game is parked in `WAITING_HOST_DECISION`. The host must intervene via `/wolf force-skip` or `/wolf extend`. This is deliberate — do **not** auto-resolve stale actions silently.
- Otherwise, reconcile Discord permissions and reattach a `GameEngine`.
- Per-game isolation: one game's failed recovery must not block others.

### Discord channel permissions

`src/wolfbot/services/permission_manager.py` reconciles three channel classes:

- **Main text** — all living players see+send; dead players see only.
- **Wolves** (private) — living werewolves see; send only during `NIGHT`.
- **Heaven** (private) — dead players see+send; living players cannot see.

The manager is idempotent: it only issues API calls on actual diffs. Don't send blind permission updates from other code paths.

### LLM integration

`src/wolfbot/services/llm_service.py` uses the `openai` client pointed at `https://api.x.ai/v1/chat/completions`. `response_format` enforces the `LLMAction` JSON schema strictly, and `tenacity` retries on transient errors.

Personas in `src/wolfbot/llm/personas.py` are **Gnosia-flavored archetypes**; `style_guide` describes only judgment tendency and tone. Hard rules enforced by the system prompt (`src/wolfbot/prompts/llm_system_prompt.md`):

- Never quote original Gnosia dialogue; imitate personality via tone only.
- No meta-commentary (no "as an AI", no referring to inputs as data).
- Japanese only, 80–300 chars per utterance.
- `target_name` must exactly match a candidate name or be `null` / intent=`skip`.

## Testing conventions

- `tests/conftest.py` provides `frozen_rng` (seed 42, deterministic role shuffles), `seats` (canonical 9-seat lineup), and async `repo` (tempfile-backed `SqliteRepo` with schema already migrated).
- For engine/timing tests use `FakeClock` from `tests/fakes.py` — do **not** mock `time.time` or `asyncio.sleep` directly.
- Per-file test lint relaxations (`B011`, `RUF001-003`) are already configured in `pyproject.toml`; no need to sprinkle `noqa`.

## Gotchas

- Python is pinned `>=3.11,<3.12`. Do not introduce 3.12+ syntax (e.g. the `type` statement).
- Ruff `E501` is ignored — the formatter handles line length. Don't hand-wrap at 100.
- SQLite foreign keys are `ON DELETE CASCADE`: deleting a `games` row cascades to seats/votes/logs.
