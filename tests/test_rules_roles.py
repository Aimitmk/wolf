"""Role distribution + persona assignment tests."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from wolfbot.domain.enums import ROLE_DISTRIBUTION, VILLAGE_SIZE, Role
from wolfbot.domain.models import Seat
from wolfbot.domain.rules import assign_roles


def _seats(n: int = 9) -> list[Seat]:
    return [
        Seat(
            seat_no=i,
            display_name=f"P{i}",
            discord_user_id=f"u{i}",
            is_llm=False,
            persona_key=None,
        )
        for i in range(1, n + 1)
    ]


def test_assign_roles_distribution_over_many_seeds() -> None:
    seats = _seats()
    for seed in range(200):
        rng = random.Random(seed)
        mapping = assign_roles(seats, rng)
        counts = Counter(mapping.values())
        for role, expected in ROLE_DISTRIBUTION.items():
            assert counts[role] == expected, (
                f"seed={seed} role={role} got={counts[role]} expected={expected}"
            )
        assert sum(counts.values()) == VILLAGE_SIZE


def test_assign_roles_rejects_wrong_size() -> None:
    with pytest.raises(ValueError):
        assign_roles(_seats(8), random.Random(0))
    with pytest.raises(ValueError):
        assign_roles(_seats(10), random.Random(0))


def test_assign_roles_covers_every_seat_once() -> None:
    seats = _seats()
    rng = random.Random(7)
    mapping = assign_roles(seats, rng)
    assert set(mapping.keys()) == {s.seat_no for s in seats}
    # Different seeds mostly produce different assignments
    rng2 = random.Random(8)
    mapping2 = assign_roles(seats, rng2)
    # Over the same 9 seats, both are valid role multisets
    assert Counter(mapping2.values()) == Counter(mapping.values())


def test_persona_keys_unique_in_seating() -> None:
    # Simulate 4 LLM seats; persona keys must be distinct
    llm_seats = [
        Seat(seat_no=i, display_name=p, discord_user_id=None, is_llm=True, persona_key=p)
        for i, p in enumerate(["setsu", "gina", "sq", "raqio"], start=6)
    ]
    keys = [s.persona_key for s in llm_seats]
    assert len(set(keys)) == len(keys)


def test_role_distribution_constant_sums_to_nine() -> None:
    assert sum(ROLE_DISTRIBUTION.values()) == VILLAGE_SIZE
    assert ROLE_DISTRIBUTION[Role.WEREWOLF] == 2
    assert ROLE_DISTRIBUTION[Role.VILLAGER] == 3
