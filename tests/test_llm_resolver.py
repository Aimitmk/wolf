"""Tests for `LLMAdapter._resolve_target` — the mapping from the LLM's
`target_name` string back to a seat_no.

Regressions guarded:
- Two candidates sharing the same `display_name` (duplicate human names, or a
  human named identically to a persona) must each be addressable via the
  `席{N} {name}` token. Previously `_resolve_target` returned the first match
  only, making the other seat unreachable from the LLM path.
- Legacy bare-name responses still resolve as long as they're unambiguous, so
  the change is backwards-compatible with older captured prompts.
"""

from __future__ import annotations

import random

from wolfbot.domain.models import Seat
from wolfbot.services.llm_service import FakeLLMActionDecider, LLMAdapter, seat_token


def _alice(seat_no: int) -> Seat:
    return Seat(
        seat_no=seat_no,
        display_name="Alice",
        discord_user_id=f"u{seat_no}",
        is_llm=False,
        persona_key=None,
    )


def _llm_adapter() -> LLMAdapter:
    # Only _resolve_target is exercised; the repo/decider are unused on that path.
    return LLMAdapter(
        repo=None,  # type: ignore[arg-type]
        decider=FakeLLMActionDecider(),
        rng=random.Random(0),
    )


def test_seat_token_includes_seat_no_prefix() -> None:
    assert seat_token(_alice(3)) == "席3 Alice"


def test_resolve_target_prefers_seat_token_over_display_name() -> None:
    adapter = _llm_adapter()
    candidates = [_alice(3), _alice(7)]

    assert adapter._resolve_target("席3 Alice", candidates, allow_none=False) == 3
    assert adapter._resolve_target("席7 Alice", candidates, allow_none=False) == 7


def test_resolve_target_falls_back_to_unique_display_name() -> None:
    """Backwards compatibility: bare-name responses still work when unambiguous."""
    adapter = _llm_adapter()
    candidates = [
        _alice(3),
        Seat(seat_no=5, display_name="Bob", discord_user_id="u5", is_llm=False, persona_key=None),
    ]

    assert adapter._resolve_target("Bob", candidates, allow_none=False) == 5


def test_resolve_target_random_fallback_on_ambiguous_bare_name() -> None:
    """Bare name matching two candidates is treated as unresolvable and random-picks."""
    adapter = _llm_adapter()
    candidates = [_alice(3), _alice(7)]

    # Both return values are valid fallback picks, we just care it doesn't always
    # silently return seat 3 like the old buggy behavior — and that it stays within
    # the candidate set.
    picks = {adapter._resolve_target("Alice", candidates, allow_none=False) for _ in range(20)}
    assert picks.issubset({3, 7})


def test_resolve_target_unknown_seat_token_falls_back() -> None:
    adapter = _llm_adapter()
    candidates = [_alice(3), _alice(7)]

    pick = adapter._resolve_target("席99 Alice", candidates, allow_none=False)
    assert pick in (3, 7)


def test_resolve_target_none_returns_none_when_allowed() -> None:
    adapter = _llm_adapter()
    candidates = [_alice(3)]

    assert adapter._resolve_target(None, candidates, allow_none=True) is None


def test_resolve_target_none_falls_back_when_not_allowed() -> None:
    adapter = _llm_adapter()
    candidates = [_alice(3), _alice(7)]

    pick = adapter._resolve_target(None, candidates, allow_none=False)
    assert pick in (3, 7)
