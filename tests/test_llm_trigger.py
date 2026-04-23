"""LLMAdapter trigger-loop behavior: day-start + on_message reactions."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Seat
from wolfbot.services.llm_service import (
    FakeLLMActionDecider,
    LLMAction,
    LLMAdapter,
)


@dataclass
class FakePoster:
    messages: list[tuple[str, str]] = field(default_factory=list)

    async def post_public(self, game: Any, text: str, kind: str) -> None:
        self.messages.append((text, kind))


class FakeGS:
    pass


async def _seed(repo):
    game = Game(
        id="g",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        heaven_channel_id="h1",
        wolves_channel_id="w1",
        created_at=0,
    )
    await repo.create_game(game)
    seats = [
        Seat(seat_no=1, display_name="Alice", discord_user_id="u1", is_llm=False, persona_key=None),
        Seat(
            seat_no=2, display_name="Setsu", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(seat_no=3, display_name="Gina", discord_user_id=None, is_llm=True, persona_key="gina"),
    ]
    for s in seats:
        await repo.insert_seat(game.id, s)
    await repo.set_player_role(game.id, 1, Role.WEREWOLF)
    await repo.set_player_role(game.id, 2, Role.SEER)
    await repo.set_player_role(game.id, 3, Role.VILLAGER)
    return game, seats


async def test_daystart_schedules_background_task_and_speaks(repo) -> None:
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        scripted=[
            LLMAction(intent="speak", public_message="今日は様子見します", reason_summary="warmup"),
            LLMAction(intent="speak", public_message="最初は共有します", reason_summary="warmup"),
        ]
    )
    clock_val = [1000]
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: clock_val[0],
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]

    players = await repo.load_players(game.id)
    # Monkey-patch sleep to zero so the test completes quickly
    original_sleep = asyncio.sleep

    async def no_sleep(_secs: float) -> None:
        await original_sleep(0)

    import wolfbot.services.llm_service as svc

    svc.asyncio.sleep = no_sleep  # type: ignore[attr-defined]

    try:
        await adapter.submit_llm_daystart_speeches(game, players, seats)
        # Wait for the background task to finish
        await asyncio.sleep(0.05)
        # Drain any remaining scheduled tasks
        for t in list(adapter._background_tasks):
            await t
    finally:
        svc.asyncio.sleep = original_sleep  # type: ignore[attr-defined]

    # Two LLMs should have posted
    assert len(poster.messages) == 2
    assert all(kind == "LLM_SPEAK" for _, kind in poster.messages)

    counts = {
        seat_no: (await repo.load_llm_speech(game.id, day=1, seat_no=seat_no))[0]
        for seat_no in (2, 3)
    }
    assert counts == {2: 1, 3: 1}


async def test_cooldown_prevents_double_speech(repo) -> None:
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="どうしよう", reason_summary="reactive")
    )
    clock_val = [2000]
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: clock_val[0],
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]

    players = await repo.load_players(game.id)
    # Pre-set last_spoke_epoch = now - 5 (cooldown not yet expired)
    await repo.increment_llm_normal_speech(game.id, day=1, seat_no=2, now_epoch=clock_val[0] - 5)

    await adapter.maybe_react_to_message(
        game, players, seats, author_seat=1, text="Setsu どう思う？"
    )
    assert poster.messages == []  # cooldown blocked


async def test_cap_blocks_fourth_speech(repo) -> None:
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="三回目以降", reason_summary="over cap")
    )
    clock_val = [3000]
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: clock_val[0],
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]

    players = await repo.load_players(game.id)
    # Pre-set 3 speeches at a time long past — cap hit but cooldown ok
    for _ in range(3):
        await repo.increment_llm_normal_speech(game.id, day=1, seat_no=2, now_epoch=100)

    await adapter.maybe_react_to_message(game, players, seats, author_seat=1, text="Setsu 発言して")
    assert poster.messages == []


async def test_reaction_only_triggered_by_matching_message(repo) -> None:
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="呼ばれた", reason_summary="matched")
    )
    clock_val = [4000]
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: clock_val[0],
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]

    players = await repo.load_players(game.id)

    await adapter.maybe_react_to_message(game, players, seats, author_seat=1, text="こんにちは全員")
    # No trigger words, no name → no posts
    assert poster.messages == []

    await adapter.maybe_react_to_message(
        game, players, seats, author_seat=1, text="Setsu、占い CO してよ"
    )
    # Setsu's name matches → at least 1 post. (Gina also matches keyword "占い".)
    assert len(poster.messages) >= 1


async def test_reaction_suppressed_outside_day_discussion(repo) -> None:
    game, seats = await _seed(repo)
    game.phase = Phase.NIGHT
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="夜でも喋るよ", reason_summary="bug")
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 5000,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.maybe_react_to_message(
        game, players, seats, author_seat=1, text="Setsu！占い結果！"
    )
    assert poster.messages == []


async def test_daystart_skipped_when_phase_changed_midway(repo) -> None:
    """If the phase flips away from DAY_DISCUSSION mid-loop (e.g. voting started or
    recovery swapped state), the remaining LLM speeches must be suppressed."""
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="もう夜なのに喋る", reason_summary="bug")
    )
    clock_val = [7000]
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: clock_val[0],
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]

    # Flip the phase in the DB to simulate "discussion ended before the LLM got
    # to speak" (e.g. force-skip, all-humans-said-enough, or a recovery into a
    # different phase).
    from wolfbot.domain.models import Transition

    await repo.apply_transition(
        game.id,
        Transition(
            next_phase=Phase.DAY_VOTE,
            next_day=game.day_number,
            new_deadline_epoch=clock_val[0] + 60,
        ),
        expected_phase=Phase.DAY_DISCUSSION,
    )

    players = await repo.load_players(game.id)
    original_sleep = asyncio.sleep

    async def no_sleep(_secs: float) -> None:
        await original_sleep(0)

    import wolfbot.services.llm_service as svc

    svc.asyncio.sleep = no_sleep  # type: ignore[attr-defined]
    try:
        await adapter.submit_llm_daystart_speeches(game, players, seats)
        await asyncio.sleep(0.05)
        for t in list(adapter._background_tasks):
            await t
    finally:
        svc.asyncio.sleep = original_sleep  # type: ignore[attr-defined]

    # Phase-guard in _run_daystart / _maybe_speak must suppress all posting.
    assert poster.messages == []


async def test_concurrent_reactions_on_same_seat_are_serialized(repo) -> None:
    """Two reactive triggers arriving simultaneously for the same LLM must not
    both pass the cap/cooldown check and double-post. With per-seat locking,
    only the first trigger should post; the second sees cooldown and bails."""
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="同時反応", reason_summary="reactive")
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 8000,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    # Two reactions concurrently, same seat (Setsu). Before the fix this produced
    # two posts and a speech count of 2 (read-check-write race).
    await asyncio.gather(
        adapter.maybe_react_to_message(game, players, seats, author_seat=1, text="Setsu 反応して"),
        adapter.maybe_react_to_message(game, players, seats, author_seat=1, text="Setsu どう思う"),
    )

    assert len(poster.messages) == 1
    count, _, _ = await repo.load_llm_speech(game.id, day=1, seat_no=2)
    assert count == 1


async def test_concurrent_reactions_on_different_seats_both_post(repo) -> None:
    """The per-seat lock is keyed on (game_id, seat_no), so two independent
    LLM seats must still be able to speak in parallel."""
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="並行発言", reason_summary="reactive")
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 8500,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    # Trigger text contains both LLM names → each reacts for itself only.
    await asyncio.gather(
        adapter.maybe_react_to_message(
            game, players, seats, author_seat=1, text="Setsu と Gina に呼びかけ"
        ),
    )

    assert len(poster.messages) == 2
    count_setsu, _, _ = await repo.load_llm_speech(game.id, day=1, seat_no=2)
    count_gina, _, _ = await repo.load_llm_speech(game.id, day=1, seat_no=3)
    assert count_setsu == 1
    assert count_gina == 1


async def test_skip_intent_means_no_post(repo) -> None:
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="skip", public_message="", reason_summary="skip this time"),
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 6000,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.maybe_react_to_message(game, players, seats, author_seat=1, text="Setsu 占い？")
    assert poster.messages == []
    # Count NOT incremented since no speech happened
    count, _, _ = await repo.load_llm_speech(game.id, day=1, seat_no=2)
    assert count == 0
