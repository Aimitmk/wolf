"""Regression tests for /wolf create race and partial-failure paths.

- High: concurrent /wolf create in the same guild could have deleted each
  other's freshly-made private channels via _create_private_channel's
  stale-purge. Per-guild asyncio.Lock now serializes the flow.
- Medium: if wolves creation fails after heaven succeeded, the heaven
  channel was leaked. Cleanup now rolls it back.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

from wolfbot.services.discord_service import WolfCog


@dataclass
class FakeChannel:
    id: int
    deleted: bool = False

    async def delete(self, reason: str = "") -> None:
        self.deleted = True


@dataclass
class FakeResponse:
    deferred: bool = False
    ephemerals: list[str] = field(default_factory=list)

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        if ephemeral:
            self.ephemerals.append(content)

    async def defer(self, thinking: bool = False) -> None:
        self.deferred = True


@dataclass
class FakeFollowup:
    messages: list[str] = field(default_factory=list)

    async def send(self, content: str) -> None:
        self.messages.append(content)


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id


class FakeUser:
    def __init__(self, user_id: int = 777) -> None:
        self.id = user_id


class FakeInteraction:
    def __init__(self, guild_id: int, user_id: int = 777) -> None:
        self.guild: Any = FakeGuild(guild_id)
        self.guild_id = guild_id
        self.user = FakeUser(user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def _build_cog(repo: Any) -> WolfCog:
    settings = MagicMock()
    settings.MAIN_TEXT_CHANNEL_ID = 100
    settings.MAIN_VOICE_CHANNEL_ID = 200
    return WolfCog(
        bot=MagicMock(),
        repo=repo,
        game_service=MagicMock(),
        discord_adapter=MagicMock(),
        llm_adapter=MagicMock(),
        registry=MagicMock(),
        settings=settings,
    )


async def test_partial_channel_failure_rolls_back_heaven(repo) -> None:
    cog = _build_cog(repo)
    heaven = FakeChannel(id=1)
    queue: list[FakeChannel | None] = [heaven, None]  # wolves fails

    async def fake_create(
        guild: Any, name: str, *, safe_to_delete_ids: set[str]
    ) -> FakeChannel | None:
        return queue.pop(0)

    cog._create_private_channel = fake_create  # type: ignore[method-assign]

    interaction = FakeInteraction(guild_id=42)
    await WolfCog.create.callback(cog, interaction)  # type: ignore[arg-type]

    assert heaven.deleted is True
    assert await repo.load_active_game_for_guild("42") is None
    assert any("失敗" in m for m in interaction.followup.messages)


async def test_concurrent_create_serializes_and_skips_loser(repo) -> None:
    cog = _build_cog(repo)
    next_id = [0]
    call_log: list[str] = []

    async def fake_create(guild: Any, name: str, *, safe_to_delete_ids: set[str]) -> FakeChannel:
        call_log.append(f"start:{name}")
        # Yield so an unlocked scenario would interleave A and B.
        await asyncio.sleep(0)
        call_log.append(f"end:{name}")
        next_id[0] += 1
        return FakeChannel(id=next_id[0])

    cog._create_private_channel = fake_create  # type: ignore[method-assign]

    inter1 = FakeInteraction(guild_id=42)
    inter2 = FakeInteraction(guild_id=42)

    await asyncio.gather(
        WolfCog.create.callback(cog, inter1),  # type: ignore[arg-type]
        WolfCog.create.callback(cog, inter2),  # type: ignore[arg-type]
    )

    # Under the per-guild lock, only the winner creates channels.
    # The loser's re-check under the lock sees the claimed game and bails.
    assert call_log == [
        "start:wolf-heaven",
        "end:wolf-heaven",
        "start:wolf-wolves",
        "end:wolf-wolves",
    ]

    game = await repo.load_active_game_for_guild("42")
    assert game is not None
    # Winner's channel IDs must point to the channels it actually created —
    # i.e. no one deleted them as "stale" mid-flight.
    assert game.heaven_channel_id in {"1", "2"}
    assert game.wolves_channel_id in {"1", "2"}
    assert game.heaven_channel_id != game.wolves_channel_id

    # One followup says "created", the other says "already exists".
    all_followups = inter1.followup.messages + inter2.followup.messages
    assert sum("ゲーム作成" in m for m in all_followups) == 1
    assert sum("既に進行中" in m for m in all_followups) == 1


async def test_different_guilds_have_independent_locks(repo) -> None:
    cog = _build_cog(repo)
    next_id = [0]

    async def fake_create(guild: Any, name: str, *, safe_to_delete_ids: set[str]) -> FakeChannel:
        next_id[0] += 1
        return FakeChannel(id=next_id[0])

    cog._create_private_channel = fake_create  # type: ignore[method-assign]

    inter_a = FakeInteraction(guild_id=1)
    inter_b = FakeInteraction(guild_id=2)

    await asyncio.gather(
        WolfCog.create.callback(cog, inter_a),  # type: ignore[arg-type]
        WolfCog.create.callback(cog, inter_b),  # type: ignore[arg-type]
    )

    assert await repo.load_active_game_for_guild("1") is not None
    assert await repo.load_active_game_for_guild("2") is not None
    # Per-guild semantics: distinct lock objects — a global lock would regress
    # multi-guild throughput.
    assert cog._create_locks["1"] is not cog._create_locks["2"]


# -----------------------------------------------------------------------
# Stale-purge safety (the review finding): _create_private_channel must only
# delete a same-named pre-existing channel if it was created by the bot in a
# prior game — i.e. its Discord id is in the guild's recorded channel history.
# -----------------------------------------------------------------------


class _FakeExistingChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name
        self.deleted = False

    async def delete(self, reason: str = "") -> None:
        self.deleted = True


class _FakeCreatedChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name


class _FakeGuildWithChannels:
    def __init__(self, guild_id: int, text_channels: list[_FakeExistingChannel]) -> None:
        self.id = guild_id
        self.text_channels = text_channels
        self.default_role = object()
        self.me = object()
        self._next_created = 7000

    async def create_text_channel(
        self, name: str, overwrites: Any = None, reason: str = ""
    ) -> _FakeCreatedChannel:
        self._next_created += 1
        return _FakeCreatedChannel(channel_id=self._next_created, name=name)


async def _seed_prior_game(
    repo: Any,
    *,
    guild_id: str,
    heaven_channel_id: str,
    wolves_channel_id: str,
) -> None:
    """Insert a past (ended) game so its channel IDs are in history."""
    from wolfbot.domain.models import Game
    from wolfbot.services.game_service import new_game_id

    game = Game(
        id=new_game_id(),
        guild_id=guild_id,
        host_user_id="h",
        main_text_channel_id="100",
        main_vc_channel_id="200",
        heaven_channel_id=heaven_channel_id,
        wolves_channel_id=wolves_channel_id,
        created_at=0,
        ended_at=1,
    )
    await repo.create_game(game)
    await repo.end_game(game.id, ended_at_epoch=1)


async def test_create_refuses_to_delete_foreign_same_named_channel(repo) -> None:
    """Admin-created `wolf-heaven` with an ID unknown to the bot must survive
    /wolf create untouched; the method must signal failure via None."""
    cog = _build_cog(repo)
    await _seed_prior_game(repo, guild_id="42", heaven_channel_id="999", wolves_channel_id="888")
    foreign = _FakeExistingChannel(channel_id=12345, name="wolf-heaven")
    guild = _FakeGuildWithChannels(guild_id=42, text_channels=[foreign])

    safe_ids = await repo.load_private_channel_ids_for_guild("42")
    result = await cog._create_private_channel(
        guild,  # type: ignore[arg-type]
        name="wolf-heaven",
        safe_to_delete_ids=safe_ids,
    )

    assert result is None
    assert foreign.deleted is False


async def test_create_deletes_channel_matching_past_bot_game(repo) -> None:
    """If a same-named channel's ID is in the bot's own history, it's safely
    recognized as a stale bot channel and purged so the new game can proceed."""
    cog = _build_cog(repo)
    await _seed_prior_game(repo, guild_id="42", heaven_channel_id="999", wolves_channel_id="888")
    stale = _FakeExistingChannel(channel_id=999, name="wolf-heaven")
    guild = _FakeGuildWithChannels(guild_id=42, text_channels=[stale])

    safe_ids = await repo.load_private_channel_ids_for_guild("42")
    result = await cog._create_private_channel(
        guild,  # type: ignore[arg-type]
        name="wolf-heaven",
        safe_to_delete_ids=safe_ids,
    )

    assert stale.deleted is True
    assert result is not None
    assert result.name == "wolf-heaven"
