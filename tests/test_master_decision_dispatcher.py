"""Master-side `NpcDecisionDispatcher` — fan-out + future resolution.

End-to-end through `dispatch_votes` / `dispatch_night_actions`:

* Online NPC's reply via `on_vote_decision` resolves the future.
* Offline NPC (no registry entry) → None, no WS send.
* Send failure → None, future is reaped.
* No reply within `request_ttl_ms` → None (timeout).

The tests use an `InMemoryNpcRegistry` and a captured-`send` list so we
exercise the dispatch path without standing up a real WS.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from wolfbot.domain.enums import Role
from wolfbot.domain.models import Player, Seat
from wolfbot.domain.ws_messages import (
    DecideVoteRequest,
    NightActionDecision,
    VoteDecision,
)
from wolfbot.master.decision_dispatcher import (
    DecisionDispatcherConfig,
    NpcDecisionDispatcher,
)
from wolfbot.master.npc_registry import InMemoryNpcRegistry


def _capture_send(buf: list[str]) -> Callable[[str], Awaitable[None]]:
    async def _send(msg: str) -> None:
        buf.append(msg)

    return _send


def _seats() -> list[Seat]:
    return [
        Seat(seat_no=1, display_name="Alice", is_llm=False, persona_key=None,
             discord_user_id="u1"),
        Seat(seat_no=2, display_name="Bob", is_llm=True, persona_key="setsu",
             discord_user_id=None),
        Seat(seat_no=3, display_name="Carol", is_llm=True, persona_key="gina",
             discord_user_id=None),
    ]


def _voters() -> list[Player]:
    return [
        Player(seat_no=2, role=Role.VILLAGER, alive=True),
        Player(seat_no=3, role=Role.WEREWOLF, alive=True),
    ]


async def test_dispatch_votes_resolves_when_npcs_reply() -> None:
    registry = InMemoryNpcRegistry()
    seat2_buf: list[str] = []
    seat3_buf: list[str] = []
    registry.register(
        npc_id="npc_seat2", discord_bot_user_id="bot2",
        supported_voices=(), version="1",
        send=_capture_send(seat2_buf), now_ms=1000, persona_key="setsu",
    )
    registry.assign("npc_seat2", seat=2, game_id="g1", phase_id="g1::day1::DAY_VOTE::1")
    registry.register(
        npc_id="npc_seat3", discord_bot_user_id="bot3",
        supported_voices=(), version="1",
        send=_capture_send(seat3_buf), now_ms=1000, persona_key="gina",
    )
    registry.assign("npc_seat3", seat=3, game_id="g1", phase_id="g1::day1::DAY_VOTE::1")

    dispatcher = NpcDecisionDispatcher(
        registry=registry,
        config=DecisionDispatcherConfig(request_ttl_ms=5_000),
        now_ms=lambda: 2_000,
    )

    async def _drive() -> dict[int, int | None]:
        return await dispatcher.dispatch_votes(
            game_id="g1", day=1, round_=0,
            voters=_voters(), seats=_seats(),
            candidate_seats=[1, 2, 3],
        )

    task = asyncio.create_task(_drive())
    # Wait until the dispatcher has sent both requests.
    for _ in range(50):
        if len(seat2_buf) and len(seat3_buf):
            break
        await asyncio.sleep(0.01)
    assert len(seat2_buf) == 1 and len(seat3_buf) == 1

    # Reply for both seats.
    for buf, target_seat, npc_id, seat_no in (
        (seat2_buf, 1, "npc_seat2", 2),
        (seat3_buf, 1, "npc_seat3", 3),
    ):
        sent = json.loads(buf[0])
        await dispatcher.on_vote_decision(
            VoteDecision(
                ts=3_000, trace_id=sent["trace_id"], request_id=sent["request_id"],
                npc_id=npc_id, seat_no=seat_no, target_seat=target_seat,
                reason_summary="test",
            )
        )

    results = await task
    assert results == {2: 1, 3: 1}


async def test_dispatch_votes_offline_seat_resolves_to_none() -> None:
    registry = InMemoryNpcRegistry()
    # Only seat 2 is online; seat 3 has no NPC.
    seat2_buf: list[str] = []
    registry.register(
        npc_id="npc_seat2", discord_bot_user_id="bot2",
        supported_voices=(), version="1",
        send=_capture_send(seat2_buf), now_ms=1000, persona_key="setsu",
    )
    registry.assign("npc_seat2", seat=2, game_id="g1", phase_id="g1::day1::DAY_VOTE::1")

    dispatcher = NpcDecisionDispatcher(
        registry=registry,
        config=DecisionDispatcherConfig(request_ttl_ms=5_000),
        now_ms=lambda: 2_000,
    )

    async def _drive() -> dict[int, int | None]:
        return await dispatcher.dispatch_votes(
            game_id="g1", day=1, round_=0,
            voters=_voters(), seats=_seats(),
            candidate_seats=[1, 2, 3],
        )

    task = asyncio.create_task(_drive())
    for _ in range(50):
        if seat2_buf:
            break
        await asyncio.sleep(0.01)
    assert len(seat2_buf) == 1

    sent = json.loads(seat2_buf[0])
    await dispatcher.on_vote_decision(
        VoteDecision(
            ts=3_000, trace_id=sent["trace_id"], request_id=sent["request_id"],
            npc_id="npc_seat2", seat_no=2, target_seat=3,
        )
    )
    results = await task
    assert results[2] == 3
    assert results[3] is None


async def test_dispatch_votes_timeout_yields_none() -> None:
    registry = InMemoryNpcRegistry()
    seat2_buf: list[str] = []
    registry.register(
        npc_id="npc_seat2", discord_bot_user_id="bot2",
        supported_voices=(), version="1",
        send=_capture_send(seat2_buf), now_ms=1000, persona_key="setsu",
    )
    registry.assign("npc_seat2", seat=2, game_id="g1", phase_id="g1::day1::DAY_VOTE::1")

    # Very short TTL so the test runs fast.
    dispatcher = NpcDecisionDispatcher(
        registry=registry,
        config=DecisionDispatcherConfig(request_ttl_ms=100),
        now_ms=lambda: 0,
    )
    voter_only = [Player(seat_no=2, role=Role.VILLAGER, alive=True)]
    results = await dispatcher.dispatch_votes(
        game_id="g1", day=1, round_=0,
        voters=voter_only, seats=_seats(),
        candidate_seats=[1, 3],
    )
    assert results == {2: None}


async def test_dispatch_votes_send_failure_yields_none() -> None:
    registry = InMemoryNpcRegistry()

    async def _failing_send(_msg: str) -> None:
        raise RuntimeError("ws_closed")

    registry.register(
        npc_id="npc_seat2", discord_bot_user_id="bot2",
        supported_voices=(), version="1",
        send=_failing_send, now_ms=1000, persona_key="setsu",
    )
    registry.assign("npc_seat2", seat=2, game_id="g1", phase_id="g1::day1::DAY_VOTE::1")

    dispatcher = NpcDecisionDispatcher(
        registry=registry,
        config=DecisionDispatcherConfig(request_ttl_ms=200),
        now_ms=lambda: 0,
    )
    voter_only = [Player(seat_no=2, role=Role.VILLAGER, alive=True)]
    results = await dispatcher.dispatch_votes(
        game_id="g1", day=1, round_=0,
        voters=voter_only, seats=_seats(),
        candidate_seats=[1, 3],
    )
    assert results == {2: None}


async def test_dispatch_night_actions_routes_action_kind() -> None:
    registry = InMemoryNpcRegistry()
    seat3_buf: list[str] = []
    registry.register(
        npc_id="npc_seat3", discord_bot_user_id="bot3",
        supported_voices=(), version="1",
        send=_capture_send(seat3_buf), now_ms=1000, persona_key="gina",
    )
    registry.assign("npc_seat3", seat=3, game_id="g1", phase_id="g1::day1::NIGHT::1")

    dispatcher = NpcDecisionDispatcher(
        registry=registry,
        config=DecisionDispatcherConfig(request_ttl_ms=5_000),
        now_ms=lambda: 0,
    )
    actor = Player(seat_no=3, role=Role.WEREWOLF, alive=True)

    async def _drive() -> dict[int, int | None]:
        return await dispatcher.dispatch_night_actions(
            game_id="g1", day=1, action_kind="wolf_attack",
            actors=[actor], seats=_seats(),
            candidate_seats=[1],
        )

    task = asyncio.create_task(_drive())
    for _ in range(50):
        if seat3_buf:
            break
        await asyncio.sleep(0.01)
    assert len(seat3_buf) == 1
    sent = json.loads(seat3_buf[0])
    assert sent["action_kind"] == "wolf_attack"
    assert sent["candidate_seats"] == [[1, "Alice"]]

    await dispatcher.on_night_action_decision(
        NightActionDecision(
            ts=2_000, trace_id=sent["trace_id"], request_id=sent["request_id"],
            npc_id="npc_seat3", seat_no=3, action_kind="wolf_attack",
            target_seat=1,
        )
    )
    results = await task
    assert results == {3: 1}


async def test_dispatch_wolf_chat_lines_resolves_sequentially() -> None:
    """`dispatch_wolf_chat_lines` runs wolves sequentially so each
    WolfChatRequest awaits the previous wolf's broker fan-out before the
    next wolf is asked."""
    from wolfbot.domain.ws_messages import WolfChatRequest, WolfChatSend

    registry = InMemoryNpcRegistry()
    seat2_buf: list[str] = []
    seat3_buf: list[str] = []
    registry.register(
        npc_id="npc_w2", discord_bot_user_id="bw2",
        supported_voices=(), version="1",
        send=_capture_send(seat2_buf), now_ms=1000, persona_key="setsu",
    )
    registry.assign("npc_w2", seat=2, game_id="g1", phase_id="g1::day1::NIGHT::1")
    registry.register(
        npc_id="npc_w3", discord_bot_user_id="bw3",
        supported_voices=(), version="1",
        send=_capture_send(seat3_buf), now_ms=1000, persona_key="gina",
    )
    registry.assign("npc_w3", seat=3, game_id="g1", phase_id="g1::day1::NIGHT::1")

    dispatcher = NpcDecisionDispatcher(
        registry=registry,
        config=DecisionDispatcherConfig(request_ttl_ms=5_000),
        now_ms=lambda: 1_000,
    )
    wolves = [
        Player(seat_no=2, role=Role.WEREWOLF, alive=True),
        Player(seat_no=3, role=Role.WEREWOLF, alive=True),
    ]

    async def _drive() -> dict[int, str | None]:
        return await dispatcher.dispatch_wolf_chat_lines(
            game_id="g1", day=1, wolves=wolves, seats=_seats(),
            candidate_seats=[1],
        )

    task = asyncio.create_task(_drive())
    # Wait for wolf 2's request (lower seat goes first because of sort).
    for _ in range(50):
        if seat2_buf:
            break
        await asyncio.sleep(0.01)
    assert len(seat2_buf) == 1
    assert seat3_buf == []  # wolf 3 hasn't been asked yet

    sent2 = WolfChatRequest.model_validate_json(seat2_buf[0])
    await dispatcher.on_wolf_chat_send(
        WolfChatSend(
            ts=2_000, trace_id=sent2.trace_id, request_id=sent2.request_id,
            npc_id="npc_w2", seat_no=2, game_id="g1",
            text="席1を狙う",
        )
    )
    # Now wolf 3 should be asked.
    for _ in range(50):
        if seat3_buf:
            break
        await asyncio.sleep(0.01)
    assert len(seat3_buf) == 1
    sent3 = WolfChatRequest.model_validate_json(seat3_buf[0])
    await dispatcher.on_wolf_chat_send(
        WolfChatSend(
            ts=3_000, trace_id=sent3.trace_id, request_id=sent3.request_id,
            npc_id="npc_w3", seat_no=3, game_id="g1",
            text="同意",
        )
    )
    results = await task
    assert results == {2: "席1を狙う", 3: "同意"}


async def test_dispatch_wolf_chat_lines_timeout_yields_none() -> None:
    registry = InMemoryNpcRegistry()
    seat2_buf: list[str] = []
    registry.register(
        npc_id="npc_w2", discord_bot_user_id="bw2",
        supported_voices=(), version="1",
        send=_capture_send(seat2_buf), now_ms=1000, persona_key="setsu",
    )
    registry.assign("npc_w2", seat=2, game_id="g1", phase_id="g1::day1::NIGHT::1")
    dispatcher = NpcDecisionDispatcher(
        registry=registry,
        config=DecisionDispatcherConfig(request_ttl_ms=100),
        now_ms=lambda: 0,
    )
    wolves = [Player(seat_no=2, role=Role.WEREWOLF, alive=True)]
    results = await dispatcher.dispatch_wolf_chat_lines(
        game_id="g1", day=1, wolves=wolves, seats=_seats(),
        candidate_seats=[1],
    )
    assert results == {2: None}


async def test_decide_vote_request_payload_shape() -> None:
    """The wire payload carries the seat / round / candidate pairs and a
    deadline so the NPC bot can build its prompt without a Master DB hit."""
    registry = InMemoryNpcRegistry()
    seat2_buf: list[str] = []
    registry.register(
        npc_id="npc_seat2", discord_bot_user_id="bot2",
        supported_voices=(), version="1",
        send=_capture_send(seat2_buf), now_ms=1000, persona_key="setsu",
    )
    registry.assign("npc_seat2", seat=2, game_id="g1", phase_id="g1::day1::DAY_VOTE::1")

    dispatcher = NpcDecisionDispatcher(
        registry=registry,
        config=DecisionDispatcherConfig(request_ttl_ms=5_000),
        now_ms=lambda: 1_000,
    )

    async def _drive() -> dict[int, int | None]:
        return await dispatcher.dispatch_votes(
            game_id="g1", day=1, round_=0,
            voters=[Player(seat_no=2, role=Role.VILLAGER, alive=True)],
            seats=_seats(),
            candidate_seats=[1, 3],
        )

    task = asyncio.create_task(_drive())
    for _ in range(50):
        if seat2_buf:
            break
        await asyncio.sleep(0.01)
    sent = DecideVoteRequest.model_validate_json(seat2_buf[0])
    assert sent.seat_no == 2
    assert sent.round_ == 0
    assert sent.candidate_seats == ((1, "Alice"), (3, "Carol"))
    assert sent.expires_at_ms == 1_000 + 5_000
    # Resolve so the test task completes cleanly.
    await dispatcher.on_vote_decision(
        VoteDecision(
            ts=2_000, trace_id=sent.trace_id, request_id=sent.request_id,
            npc_id="npc_seat2", seat_no=2, target_seat=3,
        )
    )
    await task


async def test_cleanup_game_resolves_pending_for_target_game_only() -> None:
    """`cleanup_game` is wired into `_on_reactive_game_end` so a long-lived
    Master process doesn't accumulate pending decision futures across games.
    Two-game scenario: dispatch one in-flight vote per game, then cleanup g1
    and verify the g1 future resolves to None while g2's is untouched.
    """
    registry = InMemoryNpcRegistry()
    seat2_buf: list[str] = []
    seat3_buf: list[str] = []
    registry.register(
        npc_id="npc_g1_seat2", discord_bot_user_id="bot2",
        supported_voices=(), version="1",
        send=_capture_send(seat2_buf), now_ms=1000, persona_key="setsu",
    )
    registry.assign(
        "npc_g1_seat2", seat=2, game_id="g1", phase_id="g1::day1::DAY_VOTE::1"
    )
    registry.register(
        npc_id="npc_g2_seat3", discord_bot_user_id="bot3",
        supported_voices=(), version="1",
        send=_capture_send(seat3_buf), now_ms=1000, persona_key="gina",
    )
    registry.assign(
        "npc_g2_seat3", seat=3, game_id="g2", phase_id="g2::day1::DAY_VOTE::1"
    )

    dispatcher = NpcDecisionDispatcher(
        registry=registry,
        # Long TTL so the test isn't racing the timeout path; cleanup must
        # win regardless.
        config=DecisionDispatcherConfig(request_ttl_ms=60_000),
        now_ms=lambda: 1_000,
    )

    async def _drive(game_id: str, voter_seat: int) -> dict[int, int | None]:
        return await dispatcher.dispatch_votes(
            game_id=game_id, day=1, round_=0,
            voters=[Player(seat_no=voter_seat, role=Role.VILLAGER, alive=True)],
            seats=_seats(),
            candidate_seats=[1, 2, 3],
        )

    g1_task = asyncio.create_task(_drive("g1", 2))
    g2_task = asyncio.create_task(_drive("g2", 3))
    # Wait until both requests have been sent.
    for _ in range(100):
        if seat2_buf and seat3_buf:
            break
        await asyncio.sleep(0.01)
    assert seat2_buf and seat3_buf

    assert len(dispatcher._pending) == 2

    swept = dispatcher.cleanup_game("g1")
    assert swept == 1
    assert len(dispatcher._pending) == 1

    g1_result = await g1_task
    assert g1_result == {2: None}, "g1 voter resolves to abstain after cleanup"
    assert not g2_task.done(), "g2 future must not be touched by g1 cleanup"

    # Resolve g2 normally so the test exits cleanly.
    sent_g2 = DecideVoteRequest.model_validate_json(seat3_buf[0])
    await dispatcher.on_vote_decision(
        VoteDecision(
            ts=2_000, trace_id=sent_g2.trace_id, request_id=sent_g2.request_id,
            npc_id="npc_g2_seat3", seat_no=3, target_seat=1,
        )
    )
    await g2_task
