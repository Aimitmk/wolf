"""Misc pure rules: day_discussion_duration, medium_detection."""

from __future__ import annotations

import pytest

from wolfbot.domain.enums import Role
from wolfbot.domain.models import Player
from wolfbot.domain.rules import (
    day_discussion_duration,
    is_detected_as_wolf,
    medium_detection,
)


def test_day_duration_progression_300_240_180_180() -> None:
    assert day_discussion_duration(1) == 300
    assert day_discussion_duration(2) == 240
    assert day_discussion_duration(3) == 180
    assert day_discussion_duration(4) == 180
    assert day_discussion_duration(10) == 180


def test_day_duration_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        day_discussion_duration(0)
    with pytest.raises(ValueError):
        day_discussion_duration(-1)


def test_medium_detection_is_binary_wolf_check() -> None:
    wolf = Player(seat_no=1, role=Role.WEREWOLF, alive=False)
    madman = Player(seat_no=3, role=Role.MADMAN, alive=False)
    seer = Player(seat_no=4, role=Role.SEER, alive=False)
    assert medium_detection(wolf) is True
    assert medium_detection(madman) is False  # madman is NOT a real wolf for medium
    assert medium_detection(seer) is False
    assert medium_detection(None) is None


def test_is_detected_as_wolf_only_true_for_werewolf() -> None:
    assert is_detected_as_wolf(Role.WEREWOLF) is True
    for role in (
        Role.MADMAN,
        Role.SEER,
        Role.MEDIUM,
        Role.KNIGHT,
        Role.VILLAGER,
        None,
    ):
        assert is_detected_as_wolf(role) is False
