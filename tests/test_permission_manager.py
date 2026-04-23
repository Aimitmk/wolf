"""PermissionManager idempotency + spec-driven permission rules.

Mocks discord.Guild / Channel / Member as minimal objects so the tests run offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Player, Seat
from wolfbot.services.permission_manager import PermissionManager


@dataclass
class FakeMember:
    id: int


@dataclass
class FakeChannel:
    id: int
    guild: Any
    _perm_calls: list[tuple[int, dict[str, Any]]] = field(default_factory=list)

    async def set_permissions(self, member: FakeMember, **overrides: Any) -> None:
        self._perm_calls.append((member.id, dict(overrides)))


@dataclass
class FakeGuild:
    id: int
    _channels: dict[int, FakeChannel] = field(default_factory=dict)
    _members: dict[int, FakeMember] = field(default_factory=dict)

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self._channels.get(channel_id)

    def get_member(self, user_id: int) -> FakeMember | None:
        return self._members.get(user_id)


@dataclass
class FakeBot:
    _guilds: dict[int, FakeGuild] = field(default_factory=dict)

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self._guilds.get(guild_id)


def _nine_seats() -> list[Seat]:
    out: list[Seat] = []
    for i in range(1, 10):
        out.append(
            Seat(
                seat_no=i,
                display_name=f"P{i}",
                discord_user_id=str(100 + i),
                is_llm=False,
                persona_key=None,
            )
        )
    return out


def _players(roles: list[Role], alive: list[bool] | None = None) -> list[Player]:
    return [
        Player(
            seat_no=i + 1, role=r, alive=True if alive is None else alive[i]
        )
        for i, r in enumerate(roles)
    ]


ROLES = [
    Role.WEREWOLF, Role.WEREWOLF, Role.MADMAN,
    Role.SEER, Role.MEDIUM, Role.KNIGHT,
    Role.VILLAGER, Role.VILLAGER, Role.VILLAGER,
]


def _setup_world() -> tuple[FakeBot, FakeGuild, dict[str, FakeChannel], Game]:
    guild = FakeGuild(id=1)
    for i in range(1, 10):
        guild._members[100 + i] = FakeMember(id=100 + i)
    main = FakeChannel(id=1001, guild=guild)
    heaven = FakeChannel(id=1002, guild=guild)
    wolves = FakeChannel(id=1003, guild=guild)
    guild._channels[1001] = main
    guild._channels[1002] = heaven
    guild._channels[1003] = wolves
    bot = FakeBot(_guilds={1: guild})
    game = Game(
        id="g",
        guild_id="1",
        host_user_id="100",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="1001",
        main_vc_channel_id="9999",
        heaven_channel_id="1002",
        wolves_channel_id="1003",
        created_at=0,
    )
    return bot, guild, {"main": main, "heaven": heaven, "wolves": wolves}, game


async def test_apply_day_grants_alive_send_dead_read_only() -> None:
    bot, _, ch, game = _setup_world()
    pm = PermissionManager(bot=bot)
    seats = _nine_seats()
    alive_flags = [True, True, True, True, False, True, True, True, True]  # seat 5 dead
    players = _players(ROLES, alive=alive_flags)

    await pm.apply(game, seats, players)

    # Main text: seat 5 should have send_messages=False; seats 1-4,6-9 True
    main_calls = dict(ch["main"]._perm_calls)
    assert main_calls[100 + 5]["send_messages"] is False
    for i in [1, 2, 3, 4, 6, 7, 8, 9]:
        assert main_calls[100 + i]["send_messages"] is True


async def test_heaven_hidden_from_alive_shown_to_dead() -> None:
    bot, _, ch, game = _setup_world()
    pm = PermissionManager(bot=bot)
    seats = _nine_seats()
    alive_flags = [True, True, True, True, False, True, True, True, True]
    players = _players(ROLES, alive=alive_flags)

    await pm.apply(game, seats, players)

    heaven_calls = dict(ch["heaven"]._perm_calls)
    assert heaven_calls[100 + 5]["view_channel"] is True
    assert heaven_calls[100 + 5]["send_messages"] is True
    for i in [1, 2, 3, 4, 6, 7, 8, 9]:
        assert heaven_calls[100 + i]["view_channel"] is False


async def test_wolves_chat_readonly_during_day() -> None:
    bot, _, ch, game = _setup_world()
    game.phase = Phase.DAY_DISCUSSION
    pm = PermissionManager(bot=bot)
    seats = _nine_seats()
    players = _players(ROLES)

    await pm.apply(game, seats, players)

    wolves_calls = dict(ch["wolves"]._perm_calls)
    # Seats 1 and 2 are wolves
    assert wolves_calls[100 + 1]["view_channel"] is True
    assert wolves_calls[100 + 1]["send_messages"] is False
    assert wolves_calls[100 + 2]["send_messages"] is False
    # Non-wolf seat 4 should NOT see it
    assert wolves_calls[100 + 4]["view_channel"] is False


async def test_wolves_chat_writable_at_night() -> None:
    bot, _, ch, game = _setup_world()
    game.phase = Phase.NIGHT
    pm = PermissionManager(bot=bot)
    seats = _nine_seats()
    players = _players(ROLES)

    await pm.apply(game, seats, players)

    wolves_calls = dict(ch["wolves"]._perm_calls)
    assert wolves_calls[100 + 1]["view_channel"] is True
    assert wolves_calls[100 + 1]["send_messages"] is True
    assert wolves_calls[100 + 2]["send_messages"] is True


async def test_dead_wolf_loses_wolves_chat_view() -> None:
    bot, _, ch, game = _setup_world()
    pm = PermissionManager(bot=bot)
    seats = _nine_seats()
    alive_flags = [True, False, True, True, True, True, True, True, True]  # wolf 2 dead
    players = _players(ROLES, alive=alive_flags)
    game.phase = Phase.NIGHT

    await pm.apply(game, seats, players)

    wolves_calls = dict(ch["wolves"]._perm_calls)
    assert wolves_calls[100 + 2]["view_channel"] is False
    assert wolves_calls[100 + 2]["send_messages"] is False


async def test_kill_flips_main_text_and_grants_heaven() -> None:
    bot, _, ch, game = _setup_world()
    pm = PermissionManager(bot=bot)
    seats = _nine_seats()

    await pm.kill(game, seats, seat_no=5, was_wolf=False)

    main_calls = dict(ch["main"]._perm_calls)
    assert main_calls[100 + 5]["send_messages"] is False
    heaven_calls = dict(ch["heaven"]._perm_calls)
    assert heaven_calls[100 + 5]["view_channel"] is True
    assert heaven_calls[100 + 5]["send_messages"] is True
    # Wolves chat unaffected for non-wolf death
    assert 100 + 5 not in dict(ch["wolves"]._perm_calls)


async def test_kill_wolf_revokes_wolves_chat_access() -> None:
    bot, _, ch, game = _setup_world()
    pm = PermissionManager(bot=bot)
    seats = _nine_seats()

    await pm.kill(game, seats, seat_no=1, was_wolf=True)

    wolves_calls = dict(ch["wolves"]._perm_calls)
    assert wolves_calls[100 + 1]["view_channel"] is False
    assert wolves_calls[100 + 1]["send_messages"] is False


async def test_llm_seats_no_discord_user_are_skipped() -> None:
    bot, _, ch, game = _setup_world()
    pm = PermissionManager(bot=bot)
    seats = [
        Seat(seat_no=1, display_name="H1", discord_user_id="101", is_llm=False, persona_key=None),
        Seat(seat_no=2, display_name="LLM2", discord_user_id=None, is_llm=True, persona_key="setsu"),
    ]
    players = [
        Player(seat_no=1, role=Role.VILLAGER, alive=True),
        Player(seat_no=2, role=Role.WEREWOLF, alive=True),
    ]
    game.phase = Phase.NIGHT
    await pm.apply(game, seats, players)

    # Only seat 1 should have been touched
    assert 101 in dict(ch["main"]._perm_calls)
    # Seat 2 (LLM) has no discord user → no call; set of user_ids on main is {101}
    assert set(dict(ch["main"]._perm_calls).keys()) == {101}


async def test_apply_is_idempotent_on_repeated_call() -> None:
    """Second apply with same state should produce same call contents (no crash)."""
    bot, _, ch, game = _setup_world()
    pm = PermissionManager(bot=bot)
    seats = _nine_seats()
    players = _players(ROLES)

    await pm.apply(game, seats, players)
    first = len(ch["main"]._perm_calls)
    await pm.apply(game, seats, players)
    second = len(ch["main"]._perm_calls)
    assert second == first * 2  # re-applied


async def test_on_game_end_clears_overwrites_with_none() -> None:
    bot, _, ch, game = _setup_world()
    pm = PermissionManager(bot=bot)
    seats = _nine_seats()

    await pm.on_game_end(game, seats)

    for key in ["main", "heaven", "wolves"]:
        calls = ch[key]._perm_calls
        # Every call should have `overwrite=None`
        for _member_id, kwargs in calls:
            assert kwargs.get("overwrite") is None
        # Every human seat should have been touched (9 seats on 3 channels)
        assert len({mid for mid, _ in calls}) == 9


async def test_apply_is_no_op_when_guild_missing() -> None:
    pm = PermissionManager(bot=FakeBot())
    game = Game(
        id="g", guild_id="999", host_user_id="h", phase=Phase.DAY_DISCUSSION,
        day_number=1, main_text_channel_id="1", main_vc_channel_id="2",
        created_at=0,
    )
    # Should not raise despite no guild
    await pm.apply(game, seats=[], players=[])
