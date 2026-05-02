"""Phase-D wolf chat broker — Master receives WolfChatSend, fans out
PrivateStateUpdate(wolf_chat) to other live wolf NPCs, and persists a
canonical WOLF_CHAT log entry."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Seat
from wolfbot.domain.ws_messages import (
    PrivateStateUpdate,
    WolfChatSend,
)
from wolfbot.master.ws.npc_registry import InMemoryNpcRegistry
from wolfbot.master.ws.wolf_chat_broker import WolfChatBroker
from wolfbot.persistence.sqlite_repo import SqliteRepo


def _capture_send(buf: list[str]) -> Callable[[str], Awaitable[None]]:
    async def _send(msg: str) -> None:
        buf.append(msg)

    return _send


async def _seed_3wolf_game(repo: SqliteRepo) -> Game:
    game = Game(
        id="g_wolf",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.NIGHT,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        wolves_channel_id="cw",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(game)
    seats = [
        Seat(seat_no=1, display_name="Alice", is_llm=True, persona_key="setsu",
             discord_user_id=None),
        Seat(seat_no=2, display_name="Bob", is_llm=True, persona_key="gina",
             discord_user_id=None),
        Seat(seat_no=3, display_name="Carol", is_llm=True, persona_key="jonas",
             discord_user_id=None),
    ]
    for s in seats:
        await repo.insert_seat(game.id, s)
    await repo.set_player_role(game.id, 1, Role.WEREWOLF)
    await repo.set_player_role(game.id, 2, Role.WEREWOLF)
    await repo.set_player_role(game.id, 3, Role.VILLAGER)
    return game


async def test_wolf_chat_send_broadcasts_to_other_wolves(repo: SqliteRepo) -> None:
    game = await _seed_3wolf_game(repo)
    registry = InMemoryNpcRegistry()
    seat1_buf: list[str] = []
    seat2_buf: list[str] = []
    seat3_buf: list[str] = []
    for npc_id, persona, seat, buf in (
        ("npc_alice", "setsu", 1, seat1_buf),
        ("npc_bob", "gina", 2, seat2_buf),
        ("npc_carol", "jonas", 3, seat3_buf),
    ):
        registry.register(
            npc_id=npc_id, discord_bot_user_id=f"bot{seat}",
            supported_voices=(), version="1",
            send=_capture_send(buf), now_ms=1000, persona_key=persona,
        )
        registry.assign(npc_id, seat=seat, game_id=game.id, phase_id="g_wolf::day1::NIGHT::1")

    broker = WolfChatBroker(registry=registry, repo=repo, now_ms=lambda: 5000)
    msg = WolfChatSend(
        ts=5000, trace_id="t",
        npc_id="npc_alice", seat_no=1, game_id=game.id,
        text="席3を狙おう",
    )
    await broker.handle_wolf_chat_send(msg)

    # Sender (seat 1) gets nothing.
    assert seat1_buf == []
    # Other live wolf (seat 2) receives a wolf_chat update.
    assert len(seat2_buf) == 1
    upd = PrivateStateUpdate.model_validate_json(seat2_buf[0])
    assert upd.update_kind == "wolf_chat"
    assert upd.payload["text"] == "席3を狙おう"
    assert upd.payload["speaker_seat"] == 1
    assert upd.payload["speaker_name"] == "Alice"
    # Non-wolf seat (3) receives nothing.
    assert seat3_buf == []

    # WOLF_CHAT log row was persisted.
    # Inspect the WOLF_CHAT private log directly via raw SQL — no
    # audience-seat filter needed since the broker writes the row with a
    # null audience (= visible to every wolf at replay time).
    async with repo._db.execute(  # type: ignore[attr-defined]
        "SELECT kind, text FROM logs_private WHERE game_id=? AND kind='WOLF_CHAT'",
        (game.id,),
    ) as cur:
        wolf_chat_rows = [dict(r) for r in await cur.fetchall()]
    assert len(wolf_chat_rows) == 1
    assert "席3を狙おう" in wolf_chat_rows[0]["text"]


async def test_wolf_chat_send_drops_non_wolf_sender(repo: SqliteRepo) -> None:
    game = await _seed_3wolf_game(repo)
    registry = InMemoryNpcRegistry()
    seat2_buf: list[str] = []
    registry.register(
        npc_id="npc_bob", discord_bot_user_id="bot2",
        supported_voices=(), version="1",
        send=_capture_send(seat2_buf), now_ms=1000, persona_key="gina",
    )
    registry.assign("npc_bob", seat=2, game_id=game.id, phase_id="g_wolf::day1::NIGHT::1")

    broker = WolfChatBroker(registry=registry, repo=repo, now_ms=lambda: 5000)
    # Seat 3 is the villager; Master must drop the impersonating message.
    msg = WolfChatSend(
        ts=5000, trace_id="t",
        npc_id="npc_carol", seat_no=3, game_id=game.id,
        text="罠を仕込む",
    )
    await broker.handle_wolf_chat_send(msg)

    assert seat2_buf == []  # No broadcast.
    async with repo._db.execute(  # type: ignore[attr-defined]
        "SELECT kind FROM logs_private WHERE game_id=? AND kind='WOLF_CHAT'",
        (game.id,),
    ) as cur:
        rows = list(await cur.fetchall())
    assert rows == []


async def test_wolf_chat_send_mirrors_to_wolves_channel_when_set(
    repo: SqliteRepo,
) -> None:
    game = await _seed_3wolf_game(repo)
    registry = InMemoryNpcRegistry()
    seat1_buf: list[str] = []
    seat2_buf: list[str] = []
    for npc_id, seat, buf in (
        ("npc_alice", 1, seat1_buf),
        ("npc_bob", 2, seat2_buf),
    ):
        registry.register(
            npc_id=npc_id, discord_bot_user_id=f"bot{seat}",
            supported_voices=(), version="1",
            send=_capture_send(buf), now_ms=1000, persona_key="x",
        )
        registry.assign(npc_id, seat=seat, game_id=game.id, phase_id="g_wolf::day1::NIGHT::1")

    posted: list[tuple[str, str]] = []

    async def _post_to_wolves(game_id: str, text: str) -> None:
        posted.append((game_id, text))

    broker = WolfChatBroker(
        registry=registry, repo=repo,
        post_to_wolves_channel=_post_to_wolves,
        now_ms=lambda: 5000,
    )
    msg = WolfChatSend(
        ts=5000, trace_id="t",
        npc_id="npc_alice", seat_no=1, game_id=game.id,
        text="様子見しよう",
    )
    await broker.handle_wolf_chat_send(msg)

    assert posted == [(game.id, "**Alice** (狼チャット): 様子見しよう")]
    # Make sure JSON validity isn't blocked by the channel mirror.
    assert "wolf_chat" in json.loads(seat2_buf[0])["update_kind"]
