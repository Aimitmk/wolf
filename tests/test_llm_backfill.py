"""Tests for `SqliteRepo.claim_start_and_backfill` — the atomic /wolf start
primitive that claims the LOBBY→SETUP transition and backfills LLM seats in a
single transaction.

Regressions this file guards:
- Seat-number assignment must yield contiguous 1..9 with no gaps (a previous
  `_next_seat_no(seats) + i` calculation placed LLMs at seat 10 and crashed
  pydantic Seat validation).
- persona_assignments must be persisted alongside the seat rows.
- Concurrent /wolf start calls must not both mutate the lobby; only the phase
  winner writes seats (prior bug: separate commits for each LLM seat let the
  race loser insert partial rows before hitting UNIQUE constraint failures).
"""

from __future__ import annotations

import asyncio
import random

from wolfbot.domain.enums import Phase
from wolfbot.domain.models import Game, Seat
from wolfbot.llm.personas import pick_personas
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.game_service import new_game_id


async def _seed_game_with_humans(repo: SqliteRepo, human_count: int) -> str:
    game = Game(
        id=new_game_id(),
        guild_id=f"g-{new_game_id()}",
        host_user_id="h",
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(game)
    for i in range(1, human_count + 1):
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
    return game.id


def _picks_as_specs(count: int, seed: int) -> list[tuple[str, str]]:
    picks = pick_personas(count, random.Random(seed))
    return [(p.display_name, p.key) for p in picks]


async def test_backfill_seven_humans_adds_two_llms_at_seats_eight_and_nine(
    repo: SqliteRepo,
) -> None:
    game_id = await _seed_game_with_humans(repo, 7)
    specs = _picks_as_specs(2, seed=42)

    ok = await repo.claim_start_and_backfill(game_id, expected_phase=Phase.LOBBY, llm_seats=specs)
    assert ok is True

    seats = await repo.load_seats(game_id)
    assert sorted(s.seat_no for s in seats) == list(range(1, 10))
    llm_seats = sorted((s for s in seats if s.is_llm), key=lambda s: s.seat_no)
    assert [s.seat_no for s in llm_seats] == [8, 9]
    assert all(s.persona_key is not None for s in llm_seats)


async def test_backfill_one_human_fills_eight_llms(repo: SqliteRepo) -> None:
    game_id = await _seed_game_with_humans(repo, 1)
    specs = _picks_as_specs(8, seed=0)

    ok = await repo.claim_start_and_backfill(game_id, expected_phase=Phase.LOBBY, llm_seats=specs)
    assert ok is True

    seats = await repo.load_seats(game_id)
    assert sorted(s.seat_no for s in seats) == list(range(1, 10))
    assert sum(1 for s in seats if s.is_llm) == 8
    for s in seats:
        if s.is_llm:
            assert s.persona_key is not None


async def test_backfill_zero_humans_fills_all_nine(repo: SqliteRepo) -> None:
    game_id = await _seed_game_with_humans(repo, 0)
    specs = _picks_as_specs(9, seed=1)

    ok = await repo.claim_start_and_backfill(game_id, expected_phase=Phase.LOBBY, llm_seats=specs)
    assert ok is True

    seats = await repo.load_seats(game_id)
    assert sorted(s.seat_no for s in seats) == list(range(1, 10))
    assert all(s.is_llm for s in seats)


async def test_backfill_no_shortfall_is_noop_on_seats_but_still_transitions(
    repo: SqliteRepo,
) -> None:
    game_id = await _seed_game_with_humans(repo, 9)

    ok = await repo.claim_start_and_backfill(game_id, expected_phase=Phase.LOBBY, llm_seats=[])
    assert ok is True

    seats = await repo.load_seats(game_id)
    assert sorted(s.seat_no for s in seats) == list(range(1, 10))
    assert all(not s.is_llm for s in seats)
    game = await repo.load_game(game_id)
    assert game is not None
    assert game.phase is Phase.SETUP


async def test_backfill_persona_assignments_are_persisted(repo: SqliteRepo) -> None:
    game_id = await _seed_game_with_humans(repo, 6)
    specs = _picks_as_specs(3, seed=123)

    ok = await repo.claim_start_and_backfill(game_id, expected_phase=Phase.LOBBY, llm_seats=specs)
    assert ok is True

    seats = await repo.load_seats(game_id)
    llm_seat_nos = [s.seat_no for s in seats if s.is_llm]
    assert llm_seat_nos == [7, 8, 9]
    persona_keys = await repo.load_persona_keys(game_id)
    for sn in llm_seat_nos:
        assert sn in persona_keys


async def test_backfill_transitions_phase_to_setup(repo: SqliteRepo) -> None:
    game_id = await _seed_game_with_humans(repo, 5)
    specs = _picks_as_specs(4, seed=7)

    game = await repo.load_game(game_id)
    assert game is not None and game.phase is Phase.LOBBY

    ok = await repo.claim_start_and_backfill(game_id, expected_phase=Phase.LOBBY, llm_seats=specs)
    assert ok is True

    game = await repo.load_game(game_id)
    assert game is not None
    assert game.phase is Phase.SETUP
    assert game.day_number == 0
    assert game.deadline_epoch is None


async def test_backfill_loses_race_when_phase_already_advanced(
    repo: SqliteRepo,
) -> None:
    """Second call (expected_phase=LOBBY) must return False without writing seats."""
    game_id = await _seed_game_with_humans(repo, 5)
    specs_a = _picks_as_specs(4, seed=11)
    specs_b = _picks_as_specs(4, seed=22)

    ok_a = await repo.claim_start_and_backfill(
        game_id, expected_phase=Phase.LOBBY, llm_seats=specs_a
    )
    assert ok_a is True

    seats_after_first = await repo.load_seats(game_id)
    assert len(seats_after_first) == 9

    ok_b = await repo.claim_start_and_backfill(
        game_id, expected_phase=Phase.LOBBY, llm_seats=specs_b
    )
    assert ok_b is False

    seats_after_second = await repo.load_seats(game_id)
    assert seats_after_second == seats_after_first  # no mutations from the loser


async def test_backfill_concurrent_calls_only_winner_writes(
    repo: SqliteRepo,
) -> None:
    """Two concurrent callers: exactly one True, one False, no IntegrityError,
    and seats end up exactly 9 without duplicates.
    """
    game_id = await _seed_game_with_humans(repo, 5)
    specs_a = _picks_as_specs(4, seed=101)
    specs_b = _picks_as_specs(4, seed=202)

    results = await asyncio.gather(
        repo.claim_start_and_backfill(game_id, expected_phase=Phase.LOBBY, llm_seats=specs_a),
        repo.claim_start_and_backfill(game_id, expected_phase=Phase.LOBBY, llm_seats=specs_b),
    )
    assert sorted(results) == [False, True]

    seats = await repo.load_seats(game_id)
    assert sorted(s.seat_no for s in seats) == list(range(1, 10))
    llm_seats = [s for s in seats if s.is_llm]
    assert len(llm_seats) == 4
    persona_keys = await repo.load_persona_keys(game_id)
    assert len(persona_keys) == 4
