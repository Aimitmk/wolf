"""Domain-level exceptions raised across layers."""

from __future__ import annotations


class ActiveGameExistsError(Exception):
    """Raised when attempting to create a second active game in a guild.

    The `games` table enforces one active (ended_at IS NULL) row per guild_id via
    a partial unique index; `SqliteRepo.create_game()` converts the resulting
    sqlite IntegrityError into this exception so callers can handle it at the
    domain level without importing sqlite3.
    """

    def __init__(self, guild_id: str) -> None:
        super().__init__(f"active game already exists for guild {guild_id}")
        self.guild_id = guild_id
