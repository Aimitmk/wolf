"""Bundle 3: rounds-mode SpeechEvent backfill — regression coverage.

Asserts that wiring a `DiscussionService` into `LLMAdapter` does NOT change
existing rounds-mode observable behavior (post counts, kind, log entries,
discussion_rounds_done progress) AND that one SpeechEvent(source=npc_generated)
is written per accepted utterance, plus exactly one phase_baseline sentinel
per phase entry. The pre-bundle behavior (no DiscussionService wired) must
remain bitwise unchanged.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

from wolfbot.domain.discussion import SpeechSource, make_phase_id
from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Seat
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discussion_service import (
    DiscussionService,
    SqliteSpeechEventStore,
)
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
        pass


@dataclass
class WakeRecorder:
    waked: list[str] = field(default_factory=list)

    def wake(self, game_id: str) -> None:
        self.waked.append(game_id)


class FakeGS:
    def __init__(self) -> None:
        self.wake = WakeRecorder()


async def _seed(repo: SqliteRepo) -> tuple[Game, list[Seat]]:
    game = Game(
        id="g-be",
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


async def test_rounds_mode_emits_speech_events_when_discussion_service_wired(
    repo: SqliteRepo,
) -> None:
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="占いCO", reason_summary="r"),
    )
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 1000,
        discussion_service=discussion,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    async def run() -> None:
        await adapter.submit_llm_discussion_rounds(game, players, seats)
        await _drain(adapter)

    await _no_sleep_block(run)

    # Existing rounds behavior preserved.
    assert len(poster.messages) == 4
    assert all(kind == "LLM_SPEAK" for _, kind in poster.messages)
    for seat_no in (2, 3):
        progress = await repo.load_llm_speech_progress(game.id, day=1, seat_no=seat_no)
        assert progress[3] == 2

    # Speech events: one phase_baseline sentinel + 4 npc_generated rows.
    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)
    events = await store.load_phase(game.id, phase_id)
    sources = [e.source for e in events]
    assert sources.count(SpeechSource.PHASE_BASELINE) == 1
    assert sources.count(SpeechSource.NPC_GENERATED) == 4
    npc_events = [e for e in events if e.source == SpeechSource.NPC_GENERATED]
    assert sorted(e.speaker_seat for e in npc_events) == [2, 2, 3, 3]
    assert all(e.text == "占いCO" for e in npc_events)


async def test_rounds_mode_without_discussion_service_unchanged(
    repo: SqliteRepo,
) -> None:
    """Smoke: when no DiscussionService is wired, no speech_events rows are
    written and the existing test_llm_trigger guarantees still hold."""
    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="発言", reason_summary="r"),
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 1000,
        discussion_service=None,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    async def run() -> None:
        await adapter.submit_llm_discussion_rounds(game, players, seats)
        await _drain(adapter)

    await _no_sleep_block(run)

    assert len(poster.messages) == 4
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)
    events = await store.load_phase(game.id, phase_id)
    assert events == []


async def test_rounds_mode_co_claims_visible_via_rebuild(repo: SqliteRepo) -> None:
    """End-to-end: backfilled SpeechEvents fold into a PublicDiscussionState
    whose `co_claims` extracts the 占いCO marker from the LLM utterance."""
    from wolfbot.services.discussion_service import rebuild_public_state_from_events

    game, seats = await _seed(repo)
    poster = FakePoster()
    decider = FakeLLMActionDecider(
        default=LLMAction(intent="speak", public_message="占いCO", reason_summary="r"),
    )
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 1000,
        discussion_service=discussion,
    )
    adapter.set_game_service(FakeGS())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    async def run() -> None:
        await adapter.submit_llm_discussion_rounds(game, players, seats)
        await _drain(adapter)

    await _no_sleep_block(run)

    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)
    events = await store.load_phase(game.id, phase_id)
    state = rebuild_public_state_from_events(events)
    assert state is not None
    assert state.alive_seat_nos == frozenset({1, 2, 3})
    # Both LLM seats CO'd; the silent set should be just the human seat.
    assert state.silent_seats == frozenset({1})
    seat_set = {c.seat for c in state.co_claims}
    assert seat_set == {2, 3}
    assert all(c.role_claim == "seer" for c in state.co_claims)
