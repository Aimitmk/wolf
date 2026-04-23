"""Tests for `SqliteRepo.join_lobby` / `SqliteRepo.leave_lobby` — the atomic
`/wolf join` and `/wolf leave` primitives.

Regression guarded: a stale join/leave executed after `/wolf start` transitioned
the game out of LOBBY used to corrupt the seat roster (missing seat, or an
IntegrityError on the replaced LLM slot). The new methods fail cleanly with a
STALE_PHASE result and leave the seat table intact.
"""

from __future__ import annotations

import random

from wolfbot.domain.enums import Phase
from wolfbot.domain.models import Game, Seat
from wolfbot.llm.personas import pick_personas
from wolfbot.persistence.sqlite_repo import (
    JoinLobbyResult,
    LeaveLobbyResult,
    SqliteRepo,
)
from wolfbot.services.game_service import new_game_id


async def _seed_empty_lobby(repo: SqliteRepo) -> str:
    game = Game(
        id=new_game_id(),
        guild_id=f"g-{new_game_id()}",
        host_user_id="h",
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(game)
    return game.id


async def _seed_lobby_with_humans(repo: SqliteRepo, human_count: int) -> str:
    game_id = await _seed_empty_lobby(repo)
    for i in range(1, human_count + 1):
        await repo.insert_seat(
            game_id,
            Seat(
                seat_no=i,
                display_name=f"H{i}",
                discord_user_id=f"u{i}",
                is_llm=False,
                persona_key=None,
            ),
        )
    return game_id


async def test_join_lobby_accepts_fresh_user(repo: SqliteRepo) -> None:
    game_id = await _seed_empty_lobby(repo)

    result, seat_no = await repo.join_lobby(
        game_id, discord_user_id="u1", display_name="Alice"
    )

    assert result is JoinLobbyResult.ACCEPTED
    assert seat_no == 1
    seats = await repo.load_seats(game_id)
    assert len(seats) == 1
    assert seats[0].discord_user_id == "u1"


async def test_join_lobby_fills_lowest_free_seat(repo: SqliteRepo) -> None:
    game_id = await _seed_lobby_with_humans(repo, 2)
    # Punch a hole at seat 1.
    await repo.delete_seat(game_id, 1)

    result, seat_no = await repo.join_lobby(
        game_id, discord_user_id="u_new", display_name="Alice"
    )

    assert result is JoinLobbyResult.ACCEPTED
    assert seat_no == 1


async def test_join_lobby_rejects_duplicate_user(repo: SqliteRepo) -> None:
    game_id = await _seed_lobby_with_humans(repo, 1)

    result, seat_no = await repo.join_lobby(
        game_id, discord_user_id="u1", display_name="Alice"
    )

    assert result is JoinLobbyResult.ALREADY_JOINED
    assert seat_no is None
    # No extra seat was inserted.
    seats = await repo.load_seats(game_id)
    assert len(seats) == 1


async def test_join_lobby_rejects_when_9_humans_present(repo: SqliteRepo) -> None:
    game_id = await _seed_lobby_with_humans(repo, 9)

    result, seat_no = await repo.join_lobby(
        game_id, discord_user_id="u_new", display_name="Alice"
    )

    assert result is JoinLobbyResult.LOBBY_FULL
    assert seat_no is None


async def test_join_lobby_rejects_stale_phase_after_start(repo: SqliteRepo) -> None:
    """Repro for the Codex v2 High finding: stale /wolf join after /wolf start."""
    game_id = await _seed_lobby_with_humans(repo, 8)
    specs = [(p.display_name, p.key) for p in pick_personas(1, random.Random(0))]
    ok = await repo.claim_start_and_backfill(
        game_id, expected_phase=Phase.LOBBY, llm_seats=specs
    )
    assert ok is True

    result, seat_no = await repo.join_lobby(
        game_id, discord_user_id="u_late", display_name="Late"
    )

    assert result is JoinLobbyResult.STALE_PHASE
    assert seat_no is None
    # Game still has exactly 9 seats, no phantom inserts.
    seats = await repo.load_seats(game_id)
    assert sorted(s.seat_no for s in seats) == list(range(1, 10))


async def test_leave_lobby_removes_existing_seat(repo: SqliteRepo) -> None:
    game_id = await _seed_lobby_with_humans(repo, 2)

    result = await repo.leave_lobby(game_id, discord_user_id="u1")

    assert result is LeaveLobbyResult.ACCEPTED
    seats = await repo.load_seats(game_id)
    assert [s.discord_user_id for s in seats] == ["u2"]


async def test_leave_lobby_rejects_non_member(repo: SqliteRepo) -> None:
    game_id = await _seed_lobby_with_humans(repo, 1)

    result = await repo.leave_lobby(game_id, discord_user_id="u_ghost")

    assert result is LeaveLobbyResult.NOT_JOINED


async def test_leave_lobby_rejects_stale_phase_after_start(repo: SqliteRepo) -> None:
    """Repro for the Codex v2 High finding: stale /wolf leave after /wolf start
    would silently drop a seat and break plan_setup's 9-seat invariant.
    """
    game_id = await _seed_lobby_with_humans(repo, 8)
    specs = [(p.display_name, p.key) for p in pick_personas(1, random.Random(0))]
    ok = await repo.claim_start_and_backfill(
        game_id, expected_phase=Phase.LOBBY, llm_seats=specs
    )
    assert ok is True
    seats_before = await repo.load_seats(game_id)
    assert sorted(s.seat_no for s in seats_before) == list(range(1, 10))

    result = await repo.leave_lobby(game_id, discord_user_id="u8")

    assert result is LeaveLobbyResult.STALE_PHASE
    seats_after = await repo.load_seats(game_id)
    assert sorted(s.seat_no for s in seats_after) == list(range(1, 10))
