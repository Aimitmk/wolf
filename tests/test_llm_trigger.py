"""LLMAdapter round-based dispatch: DAY_DISCUSSION 2-round speeches and
DAY_RUNOFF_SPEECH candidate speeches.

The reactive `maybe_react_to_message` trigger was removed in 2026-04-25 — LLMs
now speak only via these two scheduled paths.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Seat, Transition
from wolfbot.persistence.sqlite_repo import SqliteRepo
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

    async def post_wolves_chat(self, game: Any, text: str, kind: str) -> None:
        # LLMAdapter expects this method to exist on the MessagePoster
        # protocol; tests below don't post wolf chat so capture is enough.
        pass


@dataclass
class WakeRecorder:
    waked: list[str] = field(default_factory=list)

    def wake(self, game_id: str) -> None:
        self.waked.append(game_id)


class FakeGS:
    """Minimal GameService stand-in: only `wake.wake(game_id)` is called."""

    def __init__(self) -> None:
        self.wake = WakeRecorder()


async def _seed(repo: SqliteRepo) -> tuple[Game, list[Seat]]:
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
            seat_no=2, display_name="セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(seat_no=3, display_name="ジナ", discord_user_id=None, is_llm=True, persona_key="gina"),
    ]
    for s in seats:
        await repo.insert_seat(game.id, s)
    await repo.set_player_role(game.id, 1, Role.WEREWOLF)
    await repo.set_player_role(game.id, 2, Role.SEER)
    await repo.set_player_role(game.id, 3, Role.VILLAGER)
    return game, seats


async def _no_sleep_block(coro_factory: Any) -> Any:
    """Run an awaitable with `asyncio.sleep` patched to zero so jitter doesn't
    drag the test out. The patch is module-scoped so it covers nested calls
    inside _run_discussion_rounds / _run_runoff_candidate_speeches.
    """
    import wolfbot.services.llm_service as svc

    original_sleep = asyncio.sleep

    async def no_sleep(_secs: float) -> None:
        await original_sleep(0)

    svc.asyncio.sleep = no_sleep  # type: ignore[attr-defined]
    try:
        return await coro_factory()
    finally:
        svc.asyncio.sleep = original_sleep  # type: ignore[attr-defined]


async def _drain(adapter: LLMAdapter) -> None:
    await asyncio.sleep(0.05)
    for t in list(adapter._background_tasks):
        await t


# ----------------------------------- DAY_DISCUSSION 2-round dispatch
async def test_discussion_rounds_two_speeches_per_llm(repo: SqliteRepo) -> None:
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="発言です", reason_summary="r"),
    )
    gs = FakeGS()
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 1000,
    )
    adapter.set_game_service(gs)  # type: ignore[arg-type]

    players = await repo.load_players(game.id)

    async def run() -> None:
        await adapter.submit_llm_discussion_rounds(game, players, seats)
        await _drain(adapter)

    await _no_sleep_block(run)

    # 2 LLMs × 2 rounds = 4 posts, kind=LLM_SPEAK.
    assert len(poster.messages) == 4
    assert all(kind == "LLM_SPEAK" for _, kind in poster.messages)
    # Both LLM seats reach discussion_rounds_done == 2.
    for seat_no in (2, 3):
        progress = await repo.load_llm_speech_progress(game.id, day=1, seat_no=seat_no)
        assert progress[3] == 2
    # Engine wake fired once after the round task finishes.
    assert gs.wake.waked == ["g"]


async def test_discussion_rounds_seat_order(repo: SqliteRepo) -> None:
    """Round 1 finishes for all LLMs before round 2 starts; within each round
    LLMs speak in seat-no order."""
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="x", reason_summary="r"),
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 1000,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    async def run() -> None:
        await adapter.submit_llm_discussion_rounds(game, players, seats)
        await _drain(adapter)

    await _no_sleep_block(run)

    # Order: setsu (seat 2), ジナ (seat 3) — round 1 — then seat 2, seat 3 — round 2.
    speakers = [text.split("**")[1] for text, _ in poster.messages]
    assert speakers == ["セツ", "ジナ", "セツ", "ジナ"]


async def test_discussion_round_progress_persisted_on_skip(repo: SqliteRepo) -> None:
    """skip intent → no posted message, but discussion_rounds_done still advances."""
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="skip", public_message="", reason_summary="r"),
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 1000,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    async def run() -> None:
        await adapter.submit_llm_discussion_rounds(game, players, seats)
        await _drain(adapter)

    await _no_sleep_block(run)

    assert poster.messages == []
    for seat_no in (2, 3):
        progress = await repo.load_llm_speech_progress(game.id, day=1, seat_no=seat_no)
        assert progress[3] == 2


async def test_discussion_round_progress_persisted_on_decider_failure(
    repo: SqliteRepo,
) -> None:
    """If the decider raises, the round counter still advances so we don't
    freeze DAY_DISCUSSION on a transient xAI outage."""

    class _FailingDecider:
        async def decide(self, system: str, user: str) -> LLMAction:
            raise RuntimeError("xAI down")

    game, seats = await _seed(repo)
    adapter = LLMAdapter(
        repo=repo,
        decider=_FailingDecider(),  # type: ignore[arg-type]
        message_poster=FakePoster(),
        rng=random.Random(0),
        clock=lambda: 1000,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    async def run() -> None:
        await adapter.submit_llm_discussion_rounds(game, players, seats)
        await _drain(adapter)

    await _no_sleep_block(run)

    for seat_no in (2, 3):
        progress = await repo.load_llm_speech_progress(game.id, day=1, seat_no=seat_no)
        assert progress[3] == 2


async def test_discussion_rounds_skipped_when_phase_changed_midway(
    repo: SqliteRepo,
) -> None:
    """If the phase moves off DAY_DISCUSSION before/during the loop, no
    speeches should land in the public log. Per-seat reload-and-check guards
    that — even if some progress was already incremented from earlier rounds.
    """
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="夜なのに喋る", reason_summary="bug"),
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 1000,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]

    # Flip the phase before the round task starts running.
    await repo.apply_transition(
        game.id,
        Transition(
            next_phase=Phase.DAY_VOTE,
            next_day=game.day_number,
            new_deadline_epoch=1060,
        ),
        expected_phase=Phase.DAY_DISCUSSION,
    )
    players = await repo.load_players(game.id)

    async def run() -> None:
        await adapter.submit_llm_discussion_rounds(game, players, seats)
        await _drain(adapter)

    await _no_sleep_block(run)

    assert poster.messages == []


async def test_discussion_rounds_no_llms_no_dispatch(repo: SqliteRepo) -> None:
    """A village with zero alive LLMs schedules no background task."""
    game = Game(
        id="g-allhuman",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(game)
    for i in range(1, 10):
        await repo.insert_seat(
            game.id,
            Seat(
                seat_no=i,
                display_name=f"H{i}",
                discord_user_id=f"u{i}",
                is_llm=False,
                persona_key=None,
            ),
        )
    poster = FakePoster()
    adapter = LLMAdapter(
        repo=repo,
        decider=FakeLLMActionDecider(),
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 1000,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    seats = await repo.load_seats(game.id)
    players = await repo.load_players(game.id)
    await adapter.submit_llm_discussion_rounds(game, players, seats)
    assert adapter._background_tasks == set()


# ----------------------------------- DAY_RUNOFF_SPEECH dispatch
async def test_runoff_candidate_speech_only_for_tied_llm(repo: SqliteRepo) -> None:
    """Only tied LLM seats speak; non-tied LLMs are silent and untouched."""
    game, seats = await _seed(repo)
    # Move the phase to DAY_RUNOFF_SPEECH so the per-seat guard accepts the post.
    await repo.apply_transition(
        game.id,
        Transition(
            next_phase=Phase.DAY_RUNOFF_SPEECH,
            next_day=game.day_number,
            new_deadline_epoch=2000,
        ),
        expected_phase=Phase.DAY_DISCUSSION,
    )
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="決選前発言", reason_summary="r"),
    )
    gs = FakeGS()
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 1500,
    )
    adapter.set_game_service(gs)  # type: ignore[arg-type]

    players = await repo.load_players(game.id)
    # Only seat 2 (LLM) is in tied set; seat 3 (LLM) is not.
    fresh_game = await repo.load_game(game.id)
    assert fresh_game is not None

    async def run() -> None:
        await adapter.submit_llm_runoff_candidate_speeches(
            fresh_game, players, seats, tied_candidates=[2]
        )
        await _drain(adapter)

    await _no_sleep_block(run)

    assert len(poster.messages) == 1
    p2 = await repo.load_llm_speech_progress(game.id, day=1, seat_no=2)
    p3 = await repo.load_llm_speech_progress(game.id, day=1, seat_no=3)
    assert p2[4] is True
    assert p3[4] is False
    assert gs.wake.waked == ["g"]


async def test_runoff_speech_progress_persisted_on_failure(repo: SqliteRepo) -> None:
    """Decider failure during runoff speech still marks runoff_speech_done so
    the engine can advance out of DAY_RUNOFF_SPEECH."""

    class _FailingDecider:
        async def decide(self, system: str, user: str) -> LLMAction:
            raise RuntimeError("xAI down")

    game, seats = await _seed(repo)
    await repo.apply_transition(
        game.id,
        Transition(
            next_phase=Phase.DAY_RUNOFF_SPEECH,
            next_day=game.day_number,
            new_deadline_epoch=2000,
        ),
        expected_phase=Phase.DAY_DISCUSSION,
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=_FailingDecider(),  # type: ignore[arg-type]
        message_poster=FakePoster(),
        rng=random.Random(0),
        clock=lambda: 1500,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)
    fresh_game = await repo.load_game(game.id)
    assert fresh_game is not None

    async def run() -> None:
        await adapter.submit_llm_runoff_candidate_speeches(
            fresh_game, players, seats, tied_candidates=[2, 3]
        )
        await _drain(adapter)

    await _no_sleep_block(run)

    p2 = await repo.load_llm_speech_progress(game.id, day=1, seat_no=2)
    p3 = await repo.load_llm_speech_progress(game.id, day=1, seat_no=3)
    assert p2[4] is True
    assert p3[4] is True


async def test_runoff_speech_skipped_when_phase_changed(repo: SqliteRepo) -> None:
    """If the phase already advanced past DAY_RUNOFF_SPEECH, no posts."""
    game, seats = await _seed(repo)
    poster = FakePoster()
    adapter = LLMAdapter(
        repo=repo,
        decider=FakeLLMActionDecider(
            default=LLMAction(intent="speak", public_message="!", reason_summary="r"),
        ),
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 1500,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    # Game is still DAY_DISCUSSION (not DAY_RUNOFF_SPEECH).
    players = await repo.load_players(game.id)

    async def run() -> None:
        await adapter.submit_llm_runoff_candidate_speeches(
            game, players, seats, tied_candidates=[2]
        )
        await _drain(adapter)

    await _no_sleep_block(run)

    assert poster.messages == []
