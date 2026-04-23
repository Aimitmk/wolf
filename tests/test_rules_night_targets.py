"""Night legal-target rules + random white + wolf-attack resolution."""

from __future__ import annotations

import random

import pytest

from wolfbot.domain.enums import Role, SubmissionType
from wolfbot.domain.models import NightAction, Player
from wolfbot.domain.rules import (
    legal_attack_targets,
    legal_divine_targets,
    legal_guard_targets,
    random_white_target,
    resolve_wolf_attack,
)


def _players() -> list[Player]:
    roles = [
        Role.WEREWOLF, Role.WEREWOLF, Role.MADMAN,
        Role.SEER, Role.MEDIUM, Role.KNIGHT,
        Role.VILLAGER, Role.VILLAGER, Role.VILLAGER,
    ]
    return [Player(seat_no=i + 1, role=r, alive=True) for i, r in enumerate(roles)]


# --------------------------------------------------------------- attack targets
def test_attack_targets_exclude_wolves_and_self() -> None:
    players = _players()
    targets = legal_attack_targets(players, actor_seat=1)
    assert 1 not in targets  # self
    assert 2 not in targets  # the other wolf
    assert set(targets) == {3, 4, 5, 6, 7, 8, 9}


def test_attack_targets_exclude_dead() -> None:
    players = _players()
    players[4].alive = False  # medium dies
    targets = legal_attack_targets(players, actor_seat=1)
    assert 5 not in targets


# --------------------------------------------------------------- divine targets
def test_divine_targets_exclude_only_self() -> None:
    players = _players()
    targets = legal_divine_targets(players, seer_seat=4)
    assert 4 not in targets
    assert set(targets) == {1, 2, 3, 5, 6, 7, 8, 9}


# --------------------------------------------------------------- guard targets
def test_knight_cannot_self_guard() -> None:
    players = _players()
    targets = legal_guard_targets(players, knight_seat=6, previous_guard_seat=None)
    assert 6 not in targets


def test_knight_cannot_repeat_guard() -> None:
    players = _players()
    targets = legal_guard_targets(players, knight_seat=6, previous_guard_seat=3)
    assert 3 not in targets
    assert 6 not in targets
    assert set(targets) == {1, 2, 4, 5, 7, 8, 9}


def test_knight_no_previous_leaves_all_others_available() -> None:
    players = _players()
    targets = legal_guard_targets(players, knight_seat=6, previous_guard_seat=None)
    assert set(targets) == {1, 2, 3, 4, 5, 7, 8, 9}


# --------------------------------------------------------------- random white
def test_random_white_excludes_self_and_wolves() -> None:
    players = _players()
    rng = random.Random(42)
    for _ in range(200):
        target = random_white_target(players, seer_seat=4, rng=rng)
        assert target != 4
        assert players[target - 1].role is not Role.WEREWOLF


def test_random_white_pool_includes_madman_and_villagers() -> None:
    players = _players()
    rng = random.Random(0)
    seen = set()
    for _ in range(400):
        seen.add(random_white_target(players, seer_seat=4, rng=rng))
    # madman (3), medium (5), knight (6), villagers (7,8,9)
    assert 3 in seen
    assert seen.issubset({3, 5, 6, 7, 8, 9})


# --------------------------------------------------------------- wolf attack resolution
def _attack(seat: int, target: int | None, day: int = 1) -> NightAction:
    return NightAction(
        game_id="g",
        day=day,
        actor_seat=seat,
        kind=SubmissionType.WOLF_ATTACK,
        target_seat=target,
        submitted_at=0,
    )


def test_wolf_attack_two_agree() -> None:
    actions = [_attack(1, 5), _attack(2, 5)]
    r = resolve_wolf_attack(actions, alive_wolf_seats=[1, 2], force_skip=False)
    assert r.target_seat == 5
    assert not r.split
    assert r.missing == ()


def test_wolf_attack_two_split_no_force_pauses() -> None:
    actions = [_attack(1, 5), _attack(2, 6)]
    r = resolve_wolf_attack(actions, alive_wolf_seats=[1, 2], force_skip=False)
    assert r.target_seat is None
    assert r.split is True


def test_wolf_attack_two_split_with_force_skip_fails() -> None:
    actions = [_attack(1, 5), _attack(2, 6)]
    r = resolve_wolf_attack(actions, alive_wolf_seats=[1, 2], force_skip=True)
    assert r.target_seat is None
    assert r.split is True


def test_wolf_attack_one_missing_no_force_returns_missing() -> None:
    actions = [_attack(1, 5)]
    r = resolve_wolf_attack(actions, alive_wolf_seats=[1, 2], force_skip=False)
    assert r.missing == (2,)
    assert r.target_seat is None


def test_wolf_attack_one_missing_with_force_treats_as_split() -> None:
    # Spec: 未提出者は行動なし individually; one wolf said X, other said no-action
    # → 差分のため 襲撃不成立
    actions = [_attack(1, 5)]
    r = resolve_wolf_attack(actions, alive_wolf_seats=[1, 2], force_skip=True)
    assert r.target_seat is None


def test_wolf_attack_solo_alive_uses_pick() -> None:
    actions = [_attack(1, 7)]
    r = resolve_wolf_attack(actions, alive_wolf_seats=[1], force_skip=False)
    assert r.target_seat == 7


def test_wolf_attack_solo_alive_missing_pauses() -> None:
    r = resolve_wolf_attack([], alive_wolf_seats=[1], force_skip=False)
    assert r.missing == (1,)
    assert r.target_seat is None


def test_wolf_attack_solo_alive_missing_with_force_is_no_attack() -> None:
    r = resolve_wolf_attack([], alive_wolf_seats=[1], force_skip=True)
    assert r.target_seat is None
    assert r.missing == (1,)
    assert r.split is False


def test_wolf_attack_no_alive_wolves() -> None:
    r = resolve_wolf_attack([], alive_wolf_seats=[], force_skip=False)
    assert r.target_seat is None
    assert not r.split


def test_random_white_raises_when_pool_empty() -> None:
    # All non-seer non-wolves are dead (contrived case)
    players = _players()
    for p in players:
        if p.role is not Role.SEER and p.role is not Role.WEREWOLF:
            p.alive = False
    rng = random.Random(0)
    with pytest.raises(RuntimeError):
        random_white_target(players, seer_seat=4, rng=rng)
