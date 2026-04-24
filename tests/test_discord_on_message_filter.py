"""Unit tests for `_main_channel_should_llm_react`.

Regression guard for the review finding that non-participants (and dead
players) could steer LLM behavior by posting to the main channel. The
helper centralizes the alive-participant gate so the wolves-channel and the
main-channel handlers apply equivalent filtering.
"""

from __future__ import annotations

from wolfbot.domain.models import Player
from wolfbot.services.discord_service import _main_channel_should_llm_react


def _p(seat_no: int, *, alive: bool = True) -> Player:
    return Player(seat_no=seat_no, alive=alive)


def test_non_participant_does_not_trigger_llm() -> None:
    players = [_p(1), _p(2)]
    assert _main_channel_should_llm_react(author_seat=None, players=players) is False


def test_dead_participant_does_not_trigger_llm() -> None:
    players = [_p(1), _p(2, alive=False)]
    assert _main_channel_should_llm_react(author_seat=2, players=players) is False


def test_alive_participant_triggers_llm() -> None:
    players = [_p(1), _p(2)]
    assert _main_channel_should_llm_react(author_seat=2, players=players) is True


def test_unknown_seat_does_not_trigger_llm() -> None:
    """Defensive: a seat_no not in the players list must not trigger either
    (shouldn't happen in practice, but the helper must not KeyError/pass)."""
    players = [_p(1), _p(2)]
    assert _main_channel_should_llm_react(author_seat=42, players=players) is False
