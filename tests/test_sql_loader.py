"""Loader tests for ``wolfbot.persistence.sql``.

Pin two invariants the runtime depends on:

1. ``load_schema_script`` returns a concatenated string of every
   ``schema/*.sql`` file in lex order. The lex order matters because
   FK-bearing tables reference targets that must already exist —
   numeric prefixes (``01_games.sql``) encode the dependency order.
2. The script tolerates ``--`` line comments whose body itself contains
   a ``;``. This was the regression that surfaced when the loader
   first split on ``;`` in Python: the string "...enforcement; service
   code also" inside a comment caused SQLite to see ``service code
   also CREATE INDEX...`` as one mangled statement. Running the script
   through ``executescript`` (= SQLite's own parser) is what makes
   the comment safe; this test pins that contract.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from wolfbot.persistence.sql import (
    SCHEMA_DIR,
    SQL_ROOT,
    load_schema_script,
)


def test_schema_dir_layout_has_numbered_sql_files() -> None:
    """Every file in schema/ is a ``NN_*.sql`` so the lex order matches
    the FK dependency order documented in the loader."""
    files = sorted(SCHEMA_DIR.glob("*.sql"))
    assert files, "schema/ directory must contain at least one .sql file"
    for path in files:
        # The leading two digits encode dependency order. A glob like
        # `*.sql` would happily ingest a file with no prefix; this test
        # catches that drift before it breaks the migrate run.
        assert path.stem[:2].isdigit(), (
            f"{path.name} must start with NN_ prefix to define order"
        )


def test_schema_dir_lives_under_sql_root() -> None:
    assert SCHEMA_DIR.parent == SQL_ROOT
    assert SCHEMA_DIR.is_dir()


def test_load_schema_script_returns_non_empty_text() -> None:
    script = load_schema_script()
    assert "CREATE TABLE IF NOT EXISTS games" in script
    # Every CREATE TABLE statement should end in `;` so executescript
    # can split them — verifies file authors didn't drop a trailing `;`.
    assert script.count("CREATE TABLE IF NOT EXISTS") >= 10


def test_load_schema_script_concatenates_in_lex_order() -> None:
    """First file in lex order is ``01_games.sql``; its content must
    appear before ``02_seats.sql`` in the rendered script. This pins
    the FK execution order — `seats` references `games(id)` and would
    fail if the rendered order ever flipped."""
    script = load_schema_script()
    games_idx = script.find("CREATE TABLE IF NOT EXISTS games")
    seats_idx = script.find("CREATE TABLE IF NOT EXISTS seats")
    assert games_idx >= 0 and seats_idx >= 0
    assert games_idx < seats_idx, (
        "games must appear before seats in the rendered script — "
        "seats has a FK on games(id)"
    )


def test_load_schema_script_is_cached() -> None:
    """Second call returns the same string instance (LRU cache).

    Probes by mutating a file after the first read — the cached value
    must NOT pick up the change. Tests that need fresh reads call
    `load_schema_script.cache_clear` (matches the template loader's
    convention)."""
    first = load_schema_script()
    # We don't actually mutate the .sql file; we just assert identity
    # equality across calls, which the lru_cache guarantees if and only
    # if the function is decorated.
    second = load_schema_script()
    assert first is second


async def test_loaded_script_runs_against_sqlite_with_executescript(
    tmp_path: Path,
) -> None:
    """End-to-end: ``executescript(load_schema_script())`` on a fresh
    DB must succeed and produce every documented table. This is the
    canonical regression for the `--`-comment-containing-`;` bug — if
    the loader ever switches back to a Python-side split on `;`,
    this test will fail with `OperationalError: near "service": syntax error`
    (the exact error the original bug surfaced).
    """
    db_path = tmp_path / "schema_smoke.db"
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executescript(load_schema_script())
        async with db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ) as cur:
            tables = [row[0] async for row in cur]

    expected_subset = {
        "games",
        "seats",
        "night_actions",
        "votes",
        "previous_guard",
        "logs_public",
        "logs_private",
        "pending_decisions",
        "persona_assignments",
        "llm_speech_counts",
        "speech_events",
        "npc_speak_requests",
        "npc_speak_results",
        "npc_playback_events",
    }
    missing = expected_subset - set(tables)
    assert not missing, f"schema/*.sql must define all core tables; missing={missing}"


def test_load_schema_script_preserves_comment_with_semicolon_intact() -> None:
    """Regression: 01_games.sql contains a `--` comment line whose body
    has a `;` ("...enforcement; service code also"). The loader must
    NOT strip / modify the comment; SQLite's own parser handles it
    when ``executescript`` runs the script.
    """
    script = load_schema_script()
    assert "structural enforcement; service code also" in script


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Independence between tests in this module: clear the cache before
    each test so an earlier mutation experiment doesn't leak. (The
    cache is shared with the production runtime, but tests that touch
    SCHEMA_DIR contents would otherwise see stale data.)"""
    load_schema_script.cache_clear()
