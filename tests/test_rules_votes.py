"""compute_vote_result and check_victory — pure."""

from __future__ import annotations

from wolfbot.domain.enums import Faction, Role
from wolfbot.domain.models import Player, Vote
from wolfbot.domain.rules import check_victory, compute_vote_result


def _vote(voter: int, target: int | None, day: int = 1, round_: int = 0) -> Vote:
    return Vote(
        game_id="g", day=day, round=round_, voter_seat=voter,
        target_seat=target, submitted_at=0,
    )


ALIVE = set(range(1, 10))


def test_single_plurality_wins() -> None:
    votes = [
        _vote(1, 7), _vote(2, 7), _vote(3, 7),
        _vote(4, 8), _vote(5, 8),
        _vote(6, 9), _vote(7, 9), _vote(8, 9), _vote(9, 1),
    ]
    r = compute_vote_result(votes, alive_seats=ALIVE)
    # tallies: 7→3, 8→2, 9→3, 1→1 → tie between 7 and 9
    assert r.executed is None
    assert r.tied == (7, 9)


def test_unique_top_executes() -> None:
    votes = [
        _vote(1, 7), _vote(2, 7), _vote(3, 7), _vote(4, 7),
        _vote(5, 8), _vote(6, 8), _vote(7, 9), _vote(8, 9), _vote(9, 1),
    ]
    r = compute_vote_result(votes, alive_seats=ALIVE)
    assert r.executed == 7
    assert r.tied == ()


def test_abstentions_ignored() -> None:
    votes = [
        _vote(1, None), _vote(2, None),
        _vote(3, 5),
    ]
    r = compute_vote_result(votes, alive_seats=ALIVE)
    assert r.executed == 5


def test_self_vote_dropped() -> None:
    votes = [_vote(1, 1), _vote(2, 1)]
    r = compute_vote_result(votes, alive_seats=ALIVE)
    # voter 1 self-voted → dropped; only 2→1 counts
    assert r.executed == 1


def test_no_valid_votes_returns_no_execution() -> None:
    votes = [_vote(1, None), _vote(2, None)]
    r = compute_vote_result(votes, alive_seats=ALIVE)
    assert r.executed is None
    assert r.tied == ()


def test_dead_voter_ignored() -> None:
    votes = [_vote(1, 7), _vote(2, 7)]
    r = compute_vote_result(votes, alive_seats={2, 7, 8})  # voter 1 not alive
    assert r.executed == 7  # only voter 2 counts


def test_runoff_only_candidates_counted() -> None:
    votes = [
        _vote(1, 5), _vote(2, 5),  # 5 not in candidates → dropped
        _vote(3, 7), _vote(4, 9),
    ]
    r = compute_vote_result(votes, alive_seats=ALIVE, candidate_seats={7, 9})
    assert r.executed is None  # 7 and 9 each got 1 vote
    assert r.tied == (7, 9)


# ---------------------------------------------------------------- victory check
def _players_with_alive(alive_by_role: dict[Role, int]) -> list[Player]:
    """Build a 9-seat roster with alive_by_role alive per role, rest dead."""
    ps: list[Player] = []
    seat = 1
    for role, count in [
        (Role.WEREWOLF, 2), (Role.MADMAN, 1), (Role.SEER, 1),
        (Role.MEDIUM, 1), (Role.KNIGHT, 1), (Role.VILLAGER, 3),
    ]:
        alive_n = alive_by_role.get(role, count)
        for _ in range(count):
            ps.append(Player(seat_no=seat, role=role, alive=alive_n > 0))
            alive_n -= 1
            seat += 1
    return ps


def test_village_wins_when_all_wolves_dead() -> None:
    players = _players_with_alive({
        Role.WEREWOLF: 0, Role.MADMAN: 1, Role.SEER: 1,
        Role.MEDIUM: 1, Role.KNIGHT: 1, Role.VILLAGER: 3,
    })
    assert check_victory(players) is Faction.VILLAGE


def test_wolves_win_when_equal_numbers() -> None:
    # 1 wolf vs 1 non-wolf alive
    players = _players_with_alive({
        Role.WEREWOLF: 1, Role.MADMAN: 0, Role.SEER: 1,
        Role.MEDIUM: 0, Role.KNIGHT: 0, Role.VILLAGER: 0,
    })
    assert check_victory(players) is Faction.WEREWOLVES


def test_wolves_win_when_majority() -> None:
    players = _players_with_alive({
        Role.WEREWOLF: 2, Role.MADMAN: 0, Role.SEER: 1,
        Role.MEDIUM: 0, Role.KNIGHT: 0, Role.VILLAGER: 0,
    })
    assert check_victory(players) is Faction.WEREWOLVES


def test_ongoing_when_village_majority() -> None:
    # 2 wolves, 1 madman, 3 villagers → 2 wolves vs 4 non-wolves. 2 < 4 → ongoing
    players = _players_with_alive({
        Role.WEREWOLF: 2, Role.MADMAN: 1, Role.SEER: 0,
        Role.MEDIUM: 0, Role.KNIGHT: 0, Role.VILLAGER: 3,
    })
    assert check_victory(players) is None


def test_madman_counted_as_non_wolf_in_victory_math() -> None:
    # 1 wolf + 1 madman alive: 1 wolf vs 1 non-wolf → wolves win (1 >= 1)
    players = _players_with_alive({
        Role.WEREWOLF: 1, Role.MADMAN: 1, Role.SEER: 0,
        Role.MEDIUM: 0, Role.KNIGHT: 0, Role.VILLAGER: 0,
    })
    assert check_victory(players) is Faction.WEREWOLVES
    # 1 wolf + 2 others (madman + villager) → 1 vs 2 → ongoing
    players = _players_with_alive({
        Role.WEREWOLF: 1, Role.MADMAN: 1, Role.SEER: 0,
        Role.MEDIUM: 0, Role.KNIGHT: 0, Role.VILLAGER: 1,
    })
    assert check_victory(players) is None
