"""Misc pure rules: day_discussion_duration, medium_result."""

from __future__ import annotations

import pytest

from wolfbot.domain.enums import Faction, Role
from wolfbot.domain.models import Player
from wolfbot.domain.rules import day_discussion_duration, medium_result


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


def test_medium_returns_faction_not_role() -> None:
    wolf = Player(seat_no=1, role=Role.WEREWOLF, alive=False)
    madman = Player(seat_no=3, role=Role.MADMAN, alive=False)
    seer = Player(seat_no=4, role=Role.SEER, alive=False)
    assert medium_result(wolf) is Faction.WEREWOLVES
    assert medium_result(madman) is Faction.WEREWOLVES  # madman faction is WEREWOLVES
    assert medium_result(seer) is Faction.VILLAGE
    assert medium_result(None) is None
