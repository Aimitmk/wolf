from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from wolfbot.domain.models import Game, Seat
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discord_service import WolfCog
from wolfbot.services.game_service import new_game_id


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
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeInteraction:
    def __init__(self, guild_id: int, user_id: int) -> None:
        self.guild: Any = FakeGuild(guild_id)
        self.guild_id = guild_id
        self.user = FakeUser(user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


async def _seed_lobby_with_humans(
    repo: SqliteRepo,
    *,
    guild_id: int,
    host_user_id: int,
    human_names: list[str],
) -> str:
    game = Game(
        id=new_game_id(),
        guild_id=str(guild_id),
        host_user_id=str(host_user_id),
        main_text_channel_id="100",
        main_vc_channel_id="200",
        heaven_channel_id="300",
        wolves_channel_id="400",
        created_at=0,
    )
    await repo.create_game(game)
    for seat_no, name in enumerate(human_names, start=1):
        await repo.insert_seat(
            game.id,
            Seat(
                seat_no=seat_no,
                display_name=name,
                discord_user_id=str(1000 + seat_no),
                is_llm=False,
                persona_key=None,
            ),
        )
    return game.id


def _build_cog(repo: SqliteRepo, *, rng: random.Random) -> tuple[WolfCog, MagicMock]:
    settings = MagicMock()
    settings.MAIN_TEXT_CHANNEL_ID = 100
    settings.MAIN_VOICE_CHANNEL_ID = 200

    game_service = MagicMock()
    game_service.advance = AsyncMock()

    registry = MagicMock()
    registry.attach = AsyncMock()

    cog = WolfCog(
        bot=MagicMock(),
        repo=repo,
        game_service=game_service,
        discord_adapter=MagicMock(),
        llm_adapter=MagicMock(),
        registry=registry,
        settings=settings,
        rng=rng,
    )
    return cog, registry


async def test_start_followup_lists_seat_order_after_llm_backfill(repo: SqliteRepo) -> None:
    game_id = await _seed_lobby_with_humans(
        repo,
        guild_id=42,
        host_user_id=777,
        human_names=[f"H{i}" for i in range(1, 8)],
    )
    cog, registry = _build_cog(repo, rng=random.Random(0))
    cog._preflight_dms = AsyncMock(return_value=[])  # type: ignore[method-assign]
    interaction = FakeInteraction(guild_id=42, user_id=777)

    with patch("wolfbot.services.discord_service.GameEngine.start", autospec=True) as start_mock:
        await WolfCog.start.callback(cog, interaction)  # type: ignore[arg-type]

    seats = await repo.load_seats(game_id)
    assert interaction.response.deferred is True
    assert registry.attach.await_count == 1
    assert start_mock.call_count == 1
    assert interaction.followup.messages == [
        "\n".join(
            [
                "🎮 ゲーム開始。参加者: 7 人 + LLM 2 人。",
                "参加者一覧:",
                *[f"席{seat.seat_no} {seat.display_name}" for seat in seats],
            ]
        )
    ]


async def test_start_followup_lists_all_humans_without_llm(repo: SqliteRepo) -> None:
    game_id = await _seed_lobby_with_humans(
        repo,
        guild_id=43,
        host_user_id=888,
        human_names=[f"Player{i}" for i in range(1, 10)],
    )
    cog, registry = _build_cog(repo, rng=random.Random(0))
    cog._preflight_dms = AsyncMock(return_value=[])  # type: ignore[method-assign]
    interaction = FakeInteraction(guild_id=43, user_id=888)

    with patch("wolfbot.services.discord_service.GameEngine.start", autospec=True) as start_mock:
        await WolfCog.start.callback(cog, interaction)  # type: ignore[arg-type]

    seats = await repo.load_seats(game_id)
    assert interaction.response.deferred is True
    assert registry.attach.await_count == 1
    assert start_mock.call_count == 1
    assert interaction.followup.messages == [
        "\n".join(
            [
                "🎮 ゲーム開始。参加者: 9 人 + LLM 0 人。",
                "参加者一覧:",
                *[f"席{seat.seat_no} {seat.display_name}" for seat in seats],
            ]
        )
    ]
