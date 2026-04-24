"""Regression tests for `migrate()` against pre-existing (old) SQLite schemas.

Older `wolfbot.db` files are missing columns added later: `games.force_skip_pending`,
`seats.dm_channel_id`, and `pending_decisions.submissions_json`. `CREATE TABLE
IF NOT EXISTS` does not retroactively add columns to an existing table, so the
boot-time `migrate()` must guard each new column with `PRAGMA table_info` and
`ALTER TABLE ADD COLUMN`. Without that, the bot starts but the next
`SqliteRepo.create_game()` fails with `OperationalError: no column named ...`.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable
from pathlib import Path

import aiosqlite

from wolfbot.domain.enums import Phase
from wolfbot.domain.models import Game
from wolfbot.persistence.schema import migrate
from wolfbot.persistence.sqlite_repo import SqliteRepo

# DDL identical to schema.py BEFORE the columns we are testing got added.
# Keeping these inline (rather than importing) is intentional — the whole
# point is to simulate a DB created against an older code revision.
_OLD_GAMES_DDL = """
CREATE TABLE games (
    id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    host_user_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    day_number INTEGER NOT NULL DEFAULT 0,
    deadline_epoch INTEGER,
    main_text_channel_id TEXT NOT NULL,
    main_vc_channel_id TEXT NOT NULL,
    heaven_channel_id TEXT,
    wolves_channel_id TEXT,
    created_at INTEGER NOT NULL,
    ended_at INTEGER
)
"""

_OLD_SEATS_DDL = """
CREATE TABLE seats (
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    seat_no INTEGER NOT NULL,
    discord_user_id TEXT,
    display_name TEXT NOT NULL,
    is_llm INTEGER NOT NULL,
    persona_key TEXT,
    role TEXT,
    alive INTEGER NOT NULL DEFAULT 1,
    death_cause TEXT,
    death_day INTEGER,
    PRIMARY KEY (game_id, seat_no)
)
"""

_OLD_PENDING_DDL = """
CREATE TABLE pending_decisions (
    game_id TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    day INTEGER NOT NULL,
    required_submission TEXT NOT NULL,
    missing_seats_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
)
"""


async def _build_old_db(db_path: Path, ddls: Iterable[str]) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        for ddl in ddls:
            await db.execute(ddl)
        await db.commit()


async def _columns_of(db_path: Path, table: str) -> set[str]:
    async with (
        aiosqlite.connect(str(db_path)) as db,
        db.execute(f"PRAGMA table_info({table})") as cur,
    ):
        return {row[1] async for row in cur}


async def test_migrate_adds_force_skip_pending_to_old_games_table() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "old.db"
        await _build_old_db(db_path, [_OLD_GAMES_DDL])
        before = await _columns_of(db_path, "games")
        assert "force_skip_pending" not in before

        await migrate(db_path)

        after = await _columns_of(db_path, "games")
        assert "force_skip_pending" in after


async def test_migrate_adds_dm_channel_id_to_old_seats_table() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "old.db"
        await _build_old_db(db_path, [_OLD_GAMES_DDL, _OLD_SEATS_DDL])
        before = await _columns_of(db_path, "seats")
        assert "dm_channel_id" not in before

        await migrate(db_path)

        after = await _columns_of(db_path, "seats")
        assert "dm_channel_id" in after


async def test_migrate_adds_submissions_json_to_old_pending_decisions_table() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "old.db"
        await _build_old_db(db_path, [_OLD_GAMES_DDL, _OLD_PENDING_DDL])
        before = await _columns_of(db_path, "pending_decisions")
        assert "submissions_json" not in before

        await migrate(db_path)

        after = await _columns_of(db_path, "pending_decisions")
        assert "submissions_json" in after


async def test_migrate_then_create_game_succeeds_on_old_db() -> None:
    """End-to-end: an old DB upgraded by migrate() must accept a fresh create_game()."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "old.db"
        await _build_old_db(db_path, [_OLD_GAMES_DDL, _OLD_SEATS_DDL, _OLD_PENDING_DDL])

        await migrate(db_path)

        repo = SqliteRepo(db_path)
        await repo.connect()
        try:
            game = Game(
                id="game-after-migrate",
                guild_id="guild-1",
                host_user_id="host-1",
                phase=Phase.LOBBY,
                main_text_channel_id="text-1",
                main_vc_channel_id="vc-1",
                created_at=1_700_000_000,
            )
            await repo.create_game(game)
            loaded = await repo.load_game(game.id)
            assert loaded is not None
            assert loaded.force_skip_pending is False
            assert await repo.load_players(game.id) == []
            assert await repo.load_seats(game.id) == []
        finally:
            await repo.close()


async def test_migrate_is_idempotent() -> None:
    """Running migrate() twice on an old DB must not raise and must not duplicate columns."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "old.db"
        await _build_old_db(db_path, [_OLD_GAMES_DDL, _OLD_SEATS_DDL, _OLD_PENDING_DDL])

        await migrate(db_path)
        first = (
            await _columns_of(db_path, "games"),
            await _columns_of(db_path, "seats"),
            await _columns_of(db_path, "pending_decisions"),
        )

        await migrate(db_path)
        second = (
            await _columns_of(db_path, "games"),
            await _columns_of(db_path, "seats"),
            await _columns_of(db_path, "pending_decisions"),
        )

        assert first == second
