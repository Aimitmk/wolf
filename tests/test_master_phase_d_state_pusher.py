"""Phase-D state pusher — derives PrivateStateUpdate fan-outs from the
state-machine's transition logs and sends them to NPC bots."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import pytest

from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import (
    Game,
    LogEntry,
    NightAction,
    Seat,
)
from wolfbot.master.state.phase_d_state_pusher import PhaseDStatePusher
from wolfbot.master.ws.npc_registry import InMemoryNpcRegistry
from wolfbot.persistence.sqlite_repo import SqliteRepo


def _capture_send(buf: list[str]) -> Callable[[str], Awaitable[None]]:
    async def _send(msg: str) -> None:
        buf.append(msg)

    return _send


@pytest.fixture
async def fxt(repo: SqliteRepo) -> tuple[Game, list[Seat], InMemoryNpcRegistry, dict[int, list[str]]]:
    game = Game(
        id="g_pusher",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
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
    await repo.set_player_role(game.id, 1, Role.SEER)
    await repo.set_player_role(game.id, 2, Role.WEREWOLF)
    await repo.set_player_role(game.id, 3, Role.KNIGHT)

    registry = InMemoryNpcRegistry()
    bufs: dict[int, list[str]] = {1: [], 2: [], 3: []}
    for npc_id, persona, seat in (
        ("npc_alice", "setsu", 1),
        ("npc_bob", "gina", 2),
        ("npc_carol", "jonas", 3),
    ):
        registry.register(
            npc_id=npc_id, discord_bot_user_id=f"bot{seat}",
            supported_voices=(), version="1",
            send=_capture_send(bufs[seat]), now_ms=1000, persona_key=persona,
        )
        registry.assign(
            npc_id, seat=seat, game_id=game.id,
            phase_id="g_pusher::day1::DAY_DISCUSSION::1",
        )
    return game, seats, registry, bufs


def _kinds_in(buf: list[str]) -> list[str]:
    out: list[str] = []
    for raw in buf:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "private_state_update":
            out.append(d["update_kind"])
    return out


def _payload_of(buf: list[str], kind: str) -> dict[str, object]:
    for raw in buf:
        d = json.loads(raw)
        if d.get("type") == "private_state_update" and d["update_kind"] == kind:
            return d["payload"]  # type: ignore[no-any-return]
    raise AssertionError(f"no {kind} update found in buf")


async def test_pusher_skips_for_rounds_mode(
    repo: SqliteRepo,
    fxt: tuple[Game, list[Seat], InMemoryNpcRegistry, dict[int, list[str]]],
) -> None:
    """Rounds-mode games never see the Phase-D fan-out."""
    game, _seats, registry, bufs = fxt
    # Force this game out of reactive_voice.
    async with repo._db.execute(  # type: ignore[attr-defined]
        "UPDATE games SET discussion_mode='rounds' WHERE id=?", (game.id,),
    ):
        pass
    await repo._db.commit()  # type: ignore[attr-defined]
    new_game = await repo.load_game(game.id)
    assert new_game is not None and new_game.discussion_mode == "rounds"

    pusher = PhaseDStatePusher(repo=repo, registry=registry, now_ms=lambda: 5000)
    await pusher.push_after_advance(
        game=new_game, prev_phase=Phase.NIGHT,
        private_logs=[], public_logs=[],
    )
    for buf in bufs.values():
        assert buf == []  # nothing pushed


async def test_pusher_emits_seer_result_with_is_wolf(
    repo: SqliteRepo,
    fxt: tuple[Game, list[Seat], InMemoryNpcRegistry, dict[int, list[str]]],
) -> None:
    game, _seats, registry, bufs = fxt
    pusher = PhaseDStatePusher(repo=repo, registry=registry, now_ms=lambda: 5000)
    seer_log = LogEntry(
        game_id=game.id, day=1, phase=Phase.NIGHT, kind="SEER_RESULT",
        actor_seat=None, audience_seat=1, visibility="PRIVATE",
        text="占い結果: 席2 Bob は 人狼 です。", created_at=1,
    )
    await pusher.push_after_advance(
        game=game, prev_phase=Phase.NIGHT,
        private_logs=[seer_log], public_logs=[],
    )
    payload = _payload_of(bufs[1], "seer_result")
    assert payload["target_seat"] == 2
    assert payload["target_name"] == "Bob"
    assert payload["is_wolf"] is True
    # Other wolves don't see the seer's private result.
    assert "seer_result" not in _kinds_in(bufs[2])


async def test_pusher_emits_seer_result_night0_marks_white(
    repo: SqliteRepo,
    fxt: tuple[Game, list[Seat], InMemoryNpcRegistry, dict[int, list[str]]],
) -> None:
    """SEER_RESULT_NIGHT0 is always white by spec; the pusher overrides
    the role lookup so a future ruleset change can't accidentally flip it."""
    game, _seats, registry, bufs = fxt
    pusher = PhaseDStatePusher(repo=repo, registry=registry, now_ms=lambda: 5000)
    night0_log = LogEntry(
        game_id=game.id, day=0, phase=Phase.NIGHT_0, kind="SEER_RESULT_NIGHT0",
        actor_seat=None, audience_seat=1, visibility="PRIVATE",
        text="初日ランダム白: 席2 Bob は 人狼ではありません。", created_at=1,
    )
    await pusher.push_after_advance(
        game=game, prev_phase=Phase.NIGHT_0,
        private_logs=[night0_log], public_logs=[],
    )
    payload = _payload_of(bufs[1], "seer_result")
    assert payload["target_seat"] == 2
    assert payload["is_wolf"] is False  # NIGHT_0 result is forced white


async def test_pusher_emits_medium_result_with_no_execution(
    repo: SqliteRepo,
    fxt: tuple[Game, list[Seat], InMemoryNpcRegistry, dict[int, list[str]]],
) -> None:
    game, _seats, registry, bufs = fxt
    pusher = PhaseDStatePusher(repo=repo, registry=registry, now_ms=lambda: 5000)
    medium_log = LogEntry(
        game_id=game.id, day=1, phase=Phase.NIGHT, kind="MEDIUM_RESULT",
        actor_seat=None, audience_seat=2, visibility="PRIVATE",
        text="本日の霊媒結果はありません(処刑なし)。", created_at=1,
    )
    await pusher.push_after_advance(
        game=game, prev_phase=Phase.NIGHT,
        private_logs=[medium_log], public_logs=[],
    )
    payload = _payload_of(bufs[2], "medium_result")
    assert payload["is_wolf"] is None
    assert payload["target_seat"] == 0


async def test_pusher_emits_guard_entry_when_knight_submitted(
    repo: SqliteRepo,
    fxt: tuple[Game, list[Seat], InMemoryNpcRegistry, dict[int, list[str]]],
) -> None:
    game, _seats, registry, bufs = fxt
    # Knight (seat 3) submitted a guard for seat 2 this night.
    await repo.insert_night_action(
        NightAction(
            game_id=game.id, day=1, actor_seat=3,
            kind=SubmissionType.KNIGHT_GUARD, target_seat=2, submitted_at=1,
        )
    )
    pusher = PhaseDStatePusher(repo=repo, registry=registry, now_ms=lambda: 5000)
    await pusher.push_after_advance(
        game=game, prev_phase=Phase.NIGHT,
        private_logs=[], public_logs=[],
    )
    payload = _payload_of(bufs[3], "guard_entry")
    assert payload["target_seat"] == 2
    assert payload["target_name"] == "Bob"
    # Only the knight gets the guard entry.
    assert "guard_entry" not in _kinds_in(bufs[1])
    assert "guard_entry" not in _kinds_in(bufs[2])


async def test_pusher_emits_guard_resolved_on_morning(
    repo: SqliteRepo,
    fxt: tuple[Game, list[Seat], InMemoryNpcRegistry, dict[int, list[str]]],
) -> None:
    game, _seats, registry, bufs = fxt
    pusher = PhaseDStatePusher(repo=repo, registry=registry, now_ms=lambda: 5000)
    # Day 2 morning, peaceful → guard worked.
    morning_log = LogEntry(
        game_id=game.id, day=2, phase=Phase.DAY_DISCUSSION, kind="MORNING",
        actor_seat=None, visibility="PUBLIC",
        text="平和な朝です。昨晩の犠牲者はいません。", created_at=1,
    )
    new_game = game.model_copy(update={"day_number": 2})
    await pusher.push_after_advance(
        game=new_game, prev_phase=Phase.NIGHT,
        private_logs=[], public_logs=[morning_log],
    )
    payload = _payload_of(bufs[3], "guard_resolved")
    assert payload["peaceful_morning"] is True
    assert payload["day"] == 1  # the guard was submitted on day 1


async def test_pusher_fans_alive_changed_and_day_advanced_to_all_alive_llms(
    repo: SqliteRepo,
    fxt: tuple[Game, list[Seat], InMemoryNpcRegistry, dict[int, list[str]]],
) -> None:
    game, _seats, registry, bufs = fxt
    pusher = PhaseDStatePusher(repo=repo, registry=registry, now_ms=lambda: 5000)
    new_game = game.model_copy(update={"day_number": 2})
    await pusher.push_after_advance(
        game=new_game, prev_phase=Phase.NIGHT,
        private_logs=[], public_logs=[],
    )
    # Every alive LLM seat got both updates.
    for seat in (1, 2, 3):
        kinds = _kinds_in(bufs[seat])
        assert "alive_changed" in kinds
        assert "day_advanced" in kinds
    # day_advanced payload carries the new day number.
    p = _payload_of(bufs[1], "day_advanced")
    assert p["day_number"] == 2
