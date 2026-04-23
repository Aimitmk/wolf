"""Idempotent Discord channel permission management.

Keeps channel overwrites in sync with internal game state. All methods can be
called repeatedly (for recovery) and will only send the API calls that actually
change state.

Spec-driven rules (per phase):
  - main text: alive players can send+read; dead players read only. (Constant
    after SETUP; updated on death.)
  - wolves chat (private): @everyone hidden. Living werewolves can view; they can
    SEND only during NIGHT. Dead werewolves lose view.
  - heaven chat (private): @everyone hidden. The bot can view/send. Dead players
    can view/send. Living players cannot view.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

import discord

from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Player, Seat

log = logging.getLogger(__name__)

MAX_CONCURRENT_OVERWRITES = 3


class PermissionManager:
    def __init__(self, bot: Any = None) -> None:
        self.bot = bot
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_OVERWRITES)

    def _guild(self, game: Game) -> Any:
        if self.bot is None:
            return None
        return self.bot.get_guild(int(game.guild_id))

    async def apply(
        self,
        game: Game,
        seats: Sequence[Seat],
        players: Sequence[Player] | None = None,
    ) -> None:
        """Apply desired permissions for the current phase across all tracked channels.

        `players` must carry role + alive + seat_no; we look them up by seat_no. If None,
        the caller expected a phase where role-based decisions aren't needed (LOBBY/SETUP).
        """
        guild = self._guild(game)
        if guild is None:
            return

        player_by_seat: dict[int, Player] = (
            {p.seat_no: p for p in players} if players is not None else {}
        )

        main = guild.get_channel(int(game.main_text_channel_id))
        if main is not None:
            await self._apply_main_text(main, seats, player_by_seat, guild)

        if game.heaven_channel_id:
            heaven = guild.get_channel(int(game.heaven_channel_id))
            if heaven is not None:
                await self._apply_heaven(heaven, seats, player_by_seat, guild)

        if game.wolves_channel_id:
            wolves_ch = guild.get_channel(int(game.wolves_channel_id))
            if wolves_ch is not None:
                await self._apply_wolves(wolves_ch, seats, player_by_seat, game.phase, guild)

    async def kill(
        self,
        game: Game,
        seats: Sequence[Seat],
        seat_no: int,
        was_wolf: bool,
    ) -> None:
        """Flip permissions for a newly-dead player."""
        guild = self._guild(game)
        if guild is None:
            return
        seat = next((s for s in seats if s.seat_no == seat_no), None)
        if seat is None or seat.discord_user_id is None:
            return
        member = guild.get_member(int(seat.discord_user_id))
        if member is None:
            return

        main = guild.get_channel(int(game.main_text_channel_id))
        if main is not None:
            await self._set_perms(main, member, send_messages=False, read_messages=True)

        if game.heaven_channel_id:
            heaven = guild.get_channel(int(game.heaven_channel_id))
            if heaven is not None:
                await self._set_perms(
                    heaven,
                    member,
                    view_channel=True,
                    send_messages=True,
                    read_messages=True,
                )

        if was_wolf and game.wolves_channel_id:
            wolves_ch = guild.get_channel(int(game.wolves_channel_id))
            if wolves_ch is not None:
                await self._set_perms(
                    wolves_ch,
                    member,
                    view_channel=False,
                    send_messages=False,
                    read_messages=False,
                )

    async def on_game_end(self, game: Game, seats: Sequence[Seat]) -> None:
        """Clean up channel state at game end.

        main text channel: this is a configured, persistent channel — only clear
        per-member overwrites so dead players don't retain read-only access for
        the next game.

        heaven / wolves channels: these carry secret history (dead-player chat,
        werewolf night chat). Delete them outright so the next game cannot read
        the previous game's messages, even if a new channel happens to get the
        same name later.
        """
        guild = self._guild(game)
        if guild is None:
            return

        main = guild.get_channel(int(game.main_text_channel_id))
        if main is not None:
            for s in seats:
                if s.discord_user_id is None:
                    continue
                member = guild.get_member(int(s.discord_user_id))
                if member is None:
                    continue
                await self._clear_perms(main, member)

        for channel_id in filter(None, [game.heaven_channel_id, game.wolves_channel_id]):
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                continue
            try:
                await channel.delete(reason="wolfbot: game end — prevent history leak")
            except Exception:
                log.exception(
                    "on_game_end: delete private channel %s failed (game %s)",
                    channel_id,
                    game.id,
                )

    # ---------------------------------------------------------- internals
    async def _apply_main_text(
        self,
        channel: Any,
        seats: Sequence[Seat],
        player_by_seat: dict[int, Player],
        guild: Any,
    ) -> None:
        for s in seats:
            if s.discord_user_id is None:
                continue
            member = guild.get_member(int(s.discord_user_id))
            if member is None:
                continue
            p = player_by_seat.get(s.seat_no)
            alive = True if p is None else bool(p.alive)
            await self._set_perms(
                channel,
                member,
                send_messages=alive,
                read_messages=True,
                view_channel=True,
            )

    async def _apply_heaven(
        self,
        channel: Any,
        seats: Sequence[Seat],
        player_by_seat: dict[int, Player],
        guild: Any,
    ) -> None:
        for s in seats:
            if s.discord_user_id is None:
                continue
            member = guild.get_member(int(s.discord_user_id))
            if member is None:
                continue
            p = player_by_seat.get(s.seat_no)
            alive = True if p is None else bool(p.alive)
            if alive:
                await self._set_perms(
                    channel,
                    member,
                    view_channel=False,
                    send_messages=False,
                    read_messages=False,
                )
            else:
                await self._set_perms(
                    channel,
                    member,
                    view_channel=True,
                    send_messages=True,
                    read_messages=True,
                )

    async def _apply_wolves(
        self,
        channel: Any,
        seats: Sequence[Seat],
        player_by_seat: dict[int, Player],
        phase: Phase,
        guild: Any,
    ) -> None:
        writable = phase is Phase.NIGHT
        for s in seats:
            if s.discord_user_id is None:
                continue
            member = guild.get_member(int(s.discord_user_id))
            if member is None:
                continue
            p = player_by_seat.get(s.seat_no)
            is_alive_wolf = bool(p is not None and p.alive and p.role is Role.WEREWOLF)
            if is_alive_wolf:
                await self._set_perms(
                    channel,
                    member,
                    view_channel=True,
                    send_messages=writable,
                    read_messages=True,
                )
            else:
                await self._set_perms(
                    channel,
                    member,
                    view_channel=False,
                    send_messages=False,
                    read_messages=False,
                )

    async def _set_perms(self, channel: Any, member: Any, **overrides: bool) -> None:
        expected = discord.PermissionOverwrite(**overrides)
        if _current_overwrite(channel, member) == expected:
            return
        async with self._sem:
            try:
                await channel.set_permissions(member, **overrides)
            except Exception:
                log.exception(
                    "set_permissions failed channel=%s member=%s",
                    getattr(channel, "id", "?"),
                    getattr(member, "id", "?"),
                )
                raise

    async def _clear_perms(self, channel: Any, member: Any) -> None:
        if _current_overwrite(channel, member) == discord.PermissionOverwrite():
            return
        async with self._sem:
            try:
                await channel.set_permissions(member, overwrite=None)
            except Exception:
                log.exception(
                    "clear_permissions failed channel=%s member=%s",
                    getattr(channel, "id", "?"),
                    getattr(member, "id", "?"),
                )


def _current_overwrite(channel: Any, member: Any) -> discord.PermissionOverwrite:
    """Return the channel's current overwrite for `member`, or an empty overwrite.

    `discord.TextChannel.overwrites_for()` always returns a `PermissionOverwrite`
    (empty when no rule exists). The AttributeError fallback is for tests or mocks
    that don't implement the method — in that case we treat current as empty so
    the diff check falls through to the API call.
    """
    reader = getattr(channel, "overwrites_for", None)
    if reader is None:
        return discord.PermissionOverwrite()
    try:
        return reader(member) or discord.PermissionOverwrite()
    except Exception:
        return discord.PermissionOverwrite()
