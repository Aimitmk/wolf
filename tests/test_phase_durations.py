"""PhaseDurations singleton + env loader tests.

Covers:

- Default values match the historical hardcoded constants (so existing
  test suite assertions like ``1000 + NIGHT_DURATION == 1000 + 90``
  remain valid).
- ``with_factor`` scales correctly and clamps to a 1-second floor for
  fast-test mode.
- ``from_env`` honors both the global factor and per-phase absolute
  overrides, with the per-phase override winning when both are set.
- ``set_phase_durations`` flips the singleton process-wide; the
  module ``__getattr__`` aliases on ``state_machine`` reflect the new
  value (the dynamic-attribute path the future UI will rely on).
- Each test resets the singleton on teardown so leakage between tests
  is impossible.
"""

from __future__ import annotations

import pytest

from wolfbot.domain import state_machine
from wolfbot.domain.durations import (
    PhaseDurations,
    current_phase_durations,
    reset_phase_durations_to_defaults,
    set_phase_durations,
)
from wolfbot.domain.rules import day_discussion_duration


@pytest.fixture(autouse=True)
def _restore_singleton() -> None:
    yield
    reset_phase_durations_to_defaults()


def test_defaults_match_historical_hardcoded_values() -> None:
    d = PhaseDurations()
    assert d.vote == 60
    assert d.runoff == 60
    assert d.night == 90
    assert d.day_discussion_grace == 30
    assert d.runoff_speech_grace == 30
    assert d.discussion_day1 == 300
    assert d.discussion_day2 == 240
    assert d.discussion_day3plus == 180


def test_discussion_for_day_matches_historical_function() -> None:
    d = PhaseDurations()
    assert d.discussion_for_day(1) == 300
    assert d.discussion_for_day(2) == 240
    assert d.discussion_for_day(3) == 180
    assert d.discussion_for_day(10) == 180


def test_discussion_for_day_rejects_non_positive_day() -> None:
    d = PhaseDurations()
    with pytest.raises(ValueError):
        d.discussion_for_day(0)
    with pytest.raises(ValueError):
        d.discussion_for_day(-1)


def test_with_factor_scales_all_fields_proportionally() -> None:
    d = PhaseDurations().with_factor(0.5)
    assert d.vote == 30
    assert d.night == 45
    assert d.discussion_day1 == 150


def test_with_factor_clamps_to_one_second_floor() -> None:
    """Tiny factors mustn't produce a 0-second deadline; the engine's
    deadline-watcher would advance immediately, sometimes before the
    LLM submission loop has even fired."""
    d = PhaseDurations().with_factor(0.001)
    assert d.vote >= 1
    assert d.night >= 1
    assert d.discussion_day1 >= 1


def test_with_factor_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        PhaseDurations().with_factor(0)
    with pytest.raises(ValueError):
        PhaseDurations().with_factor(-1)


def test_from_env_with_no_overrides_returns_defaults() -> None:
    d = PhaseDurations.from_env(env={})
    assert d == PhaseDurations()


def test_from_env_factor_scales_all_phases() -> None:
    d = PhaseDurations.from_env(env={"WOLFBOT_PHASE_DURATION_FACTOR": "0.1"})
    # 60 * 0.1 = 6
    assert d.vote == 6
    # 90 * 0.1 = 9
    assert d.night == 9


def test_from_env_per_phase_override_beats_factor() -> None:
    d = PhaseDurations.from_env(
        env={
            "WOLFBOT_PHASE_DURATION_FACTOR": "0.1",
            "WOLFBOT_VOTE_DURATION": "42",
        }
    )
    # vote was overridden to 42 absolute, but night still gets factor (90*0.1=9).
    assert d.vote == 42
    assert d.night == 9


def test_from_env_rejects_unparseable_factor() -> None:
    with pytest.raises(ValueError, match="WOLFBOT_PHASE_DURATION_FACTOR"):
        PhaseDurations.from_env(env={"WOLFBOT_PHASE_DURATION_FACTOR": "fast"})


def test_from_env_rejects_unparseable_override() -> None:
    with pytest.raises(ValueError, match="WOLFBOT_NIGHT_DURATION"):
        PhaseDurations.from_env(env={"WOLFBOT_NIGHT_DURATION": "long"})


def test_from_env_rejects_non_positive_override() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        PhaseDurations.from_env(env={"WOLFBOT_VOTE_DURATION": "0"})
    with pytest.raises(ValueError, match="must be > 0"):
        PhaseDurations.from_env(env={"WOLFBOT_VOTE_DURATION": "-5"})


def test_from_env_ignores_blank_values() -> None:
    """Empty strings in .env files (e.g. ``WOLFBOT_VOTE_DURATION=``) must
    not be treated as zero — that's the standard "I left this blank, use
    the default" pattern in this repo's env files."""
    d = PhaseDurations.from_env(env={"WOLFBOT_VOTE_DURATION": "  "})
    assert d.vote == 60


def test_set_phase_durations_swaps_singleton_globally() -> None:
    set_phase_durations(PhaseDurations(vote=5))
    assert current_phase_durations().vote == 5
    assert current_phase_durations().night == 90  # unchanged


def test_state_machine_lazy_alias_reflects_singleton() -> None:
    """``state_machine.NIGHT_DURATION`` must return the current
    singleton value, not a snapshot from import time. This is the
    dynamic-attribute path a future UI command will rely on for
    third-party callers that already imported the module."""
    assert state_machine.NIGHT_DURATION == 90
    set_phase_durations(PhaseDurations(night=7))
    assert state_machine.NIGHT_DURATION == 7


def test_state_machine_unknown_attribute_raises() -> None:
    with pytest.raises(AttributeError):
        _ = state_machine.NOT_A_REAL_DURATION  # type: ignore[attr-defined]


def test_day_discussion_duration_function_uses_singleton() -> None:
    assert day_discussion_duration(1) == 300
    set_phase_durations(PhaseDurations(discussion_day1=11))
    assert day_discussion_duration(1) == 11
    # day 2 unchanged
    assert day_discussion_duration(2) == 240


def test_master_settings_apply_phase_durations_loads_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Settings boot hook must populate the singleton end-to-end."""
    from pydantic import SecretStr

    from wolfbot.config import MasterSettings

    monkeypatch.setenv("WOLFBOT_PHASE_DURATION_FACTOR", "0.5")
    monkeypatch.setenv("WOLFBOT_NIGHT_DURATION", "13")
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        DISCORD_TOKEN=SecretStr("token"),
        DISCORD_GUILD_ID=1,
        MAIN_TEXT_CHANNEL_ID=2,
        MAIN_VOICE_CHANNEL_ID=3,
        GAMEPLAY_LLM_PROVIDER="mock",
    )
    s.apply_phase_durations()
    d = current_phase_durations()
    assert d.vote == 30  # 60 * 0.5
    assert d.night == 13  # absolute override
