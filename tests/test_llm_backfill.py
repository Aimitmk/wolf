"""Tests for `_backfill_llm_seats` — the /wolf start helper that fills
shortfall with LLM seats.

Regression: a previous `_next_seat_no(seats) + i` calculation skipped seat
numbers whenever 2+ humans were missing (e.g. 7 humans → LLMs at seat 8 and
10, triggering Seat(seat_no=10) pydantic validation error).
"""

from __future__ import annotations

import random

from wolfbot.domain.models import Game, Seat
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discord_service import _backfill_llm_seats
from wolfbot.services.game_service import new_game_id


async def _seed_game_with_humans(repo: SqliteRepo, human_count: int) -> str:
    game = Game(
        id=new_game_id(),
        guild_id="g",
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


async def test_backfill_seven_humans_adds_two_llms_at_seats_eight_and_nine(
    repo: SqliteRepo,
) -> None:
    game_id = await _seed_game_with_humans(repo, 7)
    rng = random.Random(42)

    await _backfill_llm_seats(repo, game_id, shortfall=2, rng=rng)

    seats = await repo.load_seats(game_id)
    assert sorted(s.seat_no for s in seats) == list(range(1, 10))
    llm_seats = sorted((s for s in seats if s.is_llm), key=lambda s: s.seat_no)
    assert [s.seat_no for s in llm_seats] == [8, 9]
    assert all(s.persona_key is not None for s in llm_seats)


async def test_backfill_one_human_fills_eight_llms(repo: SqliteRepo) -> None:
    game_id = await _seed_game_with_humans(repo, 1)
    rng = random.Random(0)

    await _backfill_llm_seats(repo, game_id, shortfall=8, rng=rng)

    seats = await repo.load_seats(game_id)
    assert sorted(s.seat_no for s in seats) == list(range(1, 10))
    assert sum(1 for s in seats if s.is_llm) == 8
    # persona_key is populated for every LLM seat
    for s in seats:
        if s.is_llm:
            assert s.persona_key is not None


async def test_backfill_zero_humans_fills_all_nine(repo: SqliteRepo) -> None:
    game_id = await _seed_game_with_humans(repo, 0)
    rng = random.Random(1)

    await _backfill_llm_seats(repo, game_id, shortfall=9, rng=rng)

    seats = await repo.load_seats(game_id)
    assert sorted(s.seat_no for s in seats) == list(range(1, 10))
    assert all(s.is_llm for s in seats)


async def test_backfill_no_shortfall_is_noop(repo: SqliteRepo) -> None:
    game_id = await _seed_game_with_humans(repo, 9)
    rng = random.Random(0)

    await _backfill_llm_seats(repo, game_id, shortfall=0, rng=rng)

    seats = await repo.load_seats(game_id)
    assert sorted(s.seat_no for s in seats) == list(range(1, 10))
    assert all(not s.is_llm for s in seats)


async def test_backfill_persona_assignments_are_persisted(repo: SqliteRepo) -> None:
    game_id = await _seed_game_with_humans(repo, 6)
    rng = random.Random(123)

    await _backfill_llm_seats(repo, game_id, shortfall=3, rng=rng)

    seats = await repo.load_seats(game_id)
    llm_seat_nos = [s.seat_no for s in seats if s.is_llm]
    assert llm_seat_nos == [7, 8, 9]
    persona_keys = await repo.load_persona_keys(game_id)
    for sn in llm_seat_nos:
        assert sn in persona_keys
