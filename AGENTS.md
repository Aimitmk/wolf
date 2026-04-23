# Repository Guidelines

## Project Structure & Module Organization
Runtime code lives in `src/wolfbot`. Keep pure game logic in `domain/`, orchestration in `services/`, SQLite access in `persistence/`, LLM personas and prompts in `llm/`, and Discord UI views in `ui/`. Tests live in `tests/`, with shared fixtures in `tests/conftest.py` and fakes in `tests/fakes.py`. Use `.env.example` for local settings, and read `prompts/IMPLEMENTATION_PROMPT.md` before changing roles, phases, or event ordering.

## Build, Test, and Development Commands
Use `uv` for all local work. `uv sync` installs runtime and dev dependencies. `uv run wolfbot` starts the bot with values from `.env`. `uv run pytest tests` runs the full suite; `uv run pytest tests/test_rules_votes.py` runs one module. `uv run ruff check src tests` runs lint and import checks, `uv run ruff format src tests` formats code, and `uv run mypy` runs strict type checking.

## Coding Style & Naming Conventions
Target Python 3.11 and keep 4-space indentation. Follow Ruff defaults with a 100-character line length; let `ruff format` handle wrapping instead of manual alignment. New code should be fully type-annotated because `mypy` runs in strict mode. Use `snake_case` for modules, functions, and test files such as `test_recovery.py`; use `PascalCase` for classes and Pydantic models.

## Testing Guidelines
Write `pytest` tests in files named `tests/test_<feature>.py`. Async tests run with `asyncio_mode = auto`, so do not add `@pytest.mark.asyncio` unless the project configuration changes. Prefer the existing fakes and fixtures over mocking Discord, time, or LLM clients directly. Changes to rules, transitions, recovery, or persistence should include focused regression tests.

## Commit & Pull Request Guidelines
Recent history uses short imperative commit subjects such as `Add CLAUDE.md with commands and architecture overview`; keep that style and keep commits focused. Pull requests should explain the behavior change, list manual or automated checks run, and link the relevant issue when one exists. Include screenshots or log snippets only when the change affects Discord-facing flows or recovery behavior.

## Contributor Notes
`CLAUDE.md` is the best quick reference for architecture, commands, and repository-specific constraints. Treat `domain/` as side-effect free, and keep Discord or network I/O in outer layers.
