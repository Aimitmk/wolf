"""Filesystem-resident SQL DDL for the SQLite store.

The schema directory holds one ``NN_<entity>.sql`` file per logical
table group. The numeric prefix encodes execution order (FK-bearing
tables come after the targets they reference). :func:`load_schema_ddl`
returns each statement as a separate string so the migrate runner can
execute them one at a time and keep error messages scoped.

ALTER-TABLE migrations live in Python (:func:`wolfbot.persistence.schema.migrate`)
because SQLite has no ``ADD COLUMN IF NOT EXISTS`` and the conditional
guards need ``PRAGMA table_info`` reads. The base CREATE statements,
which are inherently idempotent via ``CREATE TABLE IF NOT EXISTS``,
live here as plain ``.sql`` so they can be edited / reviewed without a
Python diff.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

SQL_ROOT: Path = Path(__file__).resolve().parent
"""Filesystem directory containing schema/ and any future migrations/."""

SCHEMA_DIR: Path = SQL_ROOT / "schema"


@cache
def load_schema_script() -> str:
    """Concatenate every ``schema/*.sql`` in lex order into one script.

    Returned as a single string suitable for ``aiosqlite``'s
    ``executescript`` — that path goes through SQLite's own statement
    splitter, which correctly skips ``--`` line comments (containing
    semicolons or otherwise) and ``/* ... */`` block comments. A naive
    Python ``split(';')`` failed on the very first comment line that
    ended in ``;`` (game ``01_games.sql`` had "...; service code also"
    in a `--` line).

    Cached because ``schema.migrate`` runs every boot and the .sql
    files are immutable per process. Tests that need a fresh read can
    call :func:`load_schema_script.cache_clear`.
    """
    chunks: list[str] = []
    for path in sorted(SCHEMA_DIR.glob("*.sql")):
        chunks.append(path.read_text(encoding="utf-8"))
    # Each file already ends in a newline; join with a blank line so the
    # rendered script stays human-readable when dumped for debugging.
    return "\n\n".join(chunks)


__all__ = ["SCHEMA_DIR", "SQL_ROOT", "load_schema_script"]
