"""LLMAction parsing + schema correctness + adapter fallback behavior."""

from __future__ import annotations

import json
import random

import pytest

from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import Game, Seat
from wolfbot.llm.personas import PERSONAS, pick_personas
from wolfbot.services.llm_service import (
    RESPONSE_SCHEMA,
    FakeLLMActionDecider,
    LLMAction,
    LLMAdapter,
)


def test_action_parses_valid_json() -> None:
    raw = json.dumps(
        {
            "intent": "speak",
            "public_message": "私は占い師です。対抗はいますか。",
            "target_name": None,
            "reason_summary": "CO を明かしたい",
            "confidence": 0.7,
        }
    )
    action = LLMAction.model_validate_json(raw)
    assert action.intent == "speak"
    assert action.target_name is None


def test_action_rejects_unknown_intent() -> None:
    from pydantic import ValidationError

    raw = json.dumps(
        {
            "intent": "sing",
            "public_message": "",
            "target_name": None,
            "reason_summary": "",
            "confidence": 0.5,
        }
    )
    with pytest.raises(ValidationError):
        LLMAction.model_validate_json(raw)


def test_response_schema_has_required_fields() -> None:
    schema = RESPONSE_SCHEMA["schema"]
    assert isinstance(schema, dict)
    assert set(schema["required"]) == {
        "intent",
        "public_message",
        "target_name",
        "reason_summary",
        "confidence",
    }
    assert schema["additionalProperties"] is False


def test_pick_personas_returns_requested_count_and_no_duplicates() -> None:
    rng = random.Random(0)
    picks = pick_personas(5, rng)
    assert len(picks) == 5
    assert len({p.key for p in picks}) == 5


def test_pick_personas_rejects_over_capacity() -> None:
    rng = random.Random(0)
    with pytest.raises(ValueError):
        pick_personas(len(PERSONAS) + 1, rng)


# ---------------------------------------------------------- LLMAdapter
class FakeGameService:
    """Just captures submit_* calls."""

    def __init__(self) -> None:
        self.votes: list[tuple[str, int, int | None, int, int]] = []
        self.nights: list[tuple[str, int, SubmissionType, int | None, int]] = []

    async def submit_vote(
        self,
        game_id: str,
        voter_seat: int,
        target_seat: int | None,
        round_: int,
        day: int,
    ) -> None:
        self.votes.append((game_id, voter_seat, target_seat, round_, day))

    async def submit_night_action(
        self,
        game_id: str,
        actor_seat: int,
        kind: SubmissionType,
        target_seat: int | None,
        day: int,
    ) -> None:
        self.nights.append((game_id, actor_seat, kind, target_seat, day))


async def _seed_game(repo):
    game = Game(
        id="g",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.NIGHT,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        heaven_channel_id="h1",
        wolves_channel_id="w1",
        created_at=0,
    )
    await repo.create_game(game)
    seats = [
        Seat(
            seat_no=1, display_name="Human1", discord_user_id="u1", is_llm=False, persona_key=None
        ),
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


async def test_llm_adapter_submits_night_action_for_seer(repo) -> None:
    """Only the seer LLM (seat 2) should fire a NIGHT action (seat 3 villager: nothing)."""
    game, seats = await _seed_game(repo)
    gs = FakeGameService()
    decider = FakeLLMActionDecider(
        scripted=[
            LLMAction(
                intent="night_action",
                target_name="Human1",
                reason_summary="hunt wolves",
                confidence=0.9,
            ),
        ]
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        rng=random.Random(0),
    )
    adapter.set_game_service(gs)  # type: ignore[arg-type]

    players = await repo.load_players(game.id)
    await adapter.submit_llm_night_actions(game, players, seats)

    assert len(gs.nights) == 1
    _, actor_seat, kind, target_seat, day = gs.nights[0]
    assert actor_seat == 2
    assert kind is SubmissionType.SEER_DIVINE
    assert target_seat == 1  # "Human1" resolved to seat 1
    assert day == game.day_number


async def test_llm_adapter_falls_back_on_invalid_target_name(repo) -> None:
    game, seats = await _seed_game(repo)
    gs = FakeGameService()
    decider = FakeLLMActionDecider(
        scripted=[
            LLMAction(
                intent="night_action", target_name="NotAnyone", reason_summary="", confidence=0.5
            ),
        ]
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        rng=random.Random(0),
    )
    adapter.set_game_service(gs)  # type: ignore[arg-type]

    players = await repo.load_players(game.id)
    await adapter.submit_llm_night_actions(game, players, seats)

    # Should still have produced a valid seat number among legal targets
    assert len(gs.nights) == 1
    target = gs.nights[0][3]
    assert target in (1, 3)  # seer's legal targets (anyone alive besides self)


async def test_llm_adapter_votes_with_name_resolution(repo) -> None:
    game, seats = await _seed_game(repo)
    game.phase = Phase.DAY_VOTE
    gs = FakeGameService()
    decider = FakeLLMActionDecider(
        scripted=[
            LLMAction(intent="vote", target_name="Human1", reason_summary="疑う", confidence=0.6),
            LLMAction(intent="vote", target_name="Gina", reason_summary="怪しい", confidence=0.5),
        ]
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        rng=random.Random(0),
    )
    adapter.set_game_service(gs)  # type: ignore[arg-type]

    players = await repo.load_players(game.id)
    await adapter.submit_llm_votes(game, players, seats, candidates=None, round_=0)

    # 2 LLMs alive (seats 2, 3) → 2 vote submissions
    assert len(gs.votes) == 2
    submissions_by_voter = {v[1]: v[2] for v in gs.votes}
    assert submissions_by_voter[2] == 1  # seat 2 → Human1
    # seat 3 tried to vote for "Gina" (itself, not in candidates) → random fallback to
    # some other alive seat (not itself)
    assert submissions_by_voter[3] != 3
    assert submissions_by_voter[3] in (1, 2)


async def test_llm_adapter_skip_intent_abstains_on_vote(repo) -> None:
    game, seats = await _seed_game(repo)
    game.phase = Phase.DAY_VOTE
    gs = FakeGameService()
    decider = FakeLLMActionDecider(
        scripted=[
            LLMAction(intent="skip", target_name=None, reason_summary="判断保留", confidence=0.2),
            LLMAction(intent="skip", target_name=None, reason_summary="判断保留", confidence=0.2),
        ]
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        rng=random.Random(0),
    )
    adapter.set_game_service(gs)  # type: ignore[arg-type]

    players = await repo.load_players(game.id)
    await adapter.submit_llm_votes(game, players, seats, candidates=None, round_=0)
    # Both LLMs abstained → target None preserved
    assert all(v[2] is None for v in gs.votes)
