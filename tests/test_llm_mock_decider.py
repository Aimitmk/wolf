"""MockLLMActionDecider behavior tests.

Asserts that the offline-mock gameplay decider returns the right intent
based on the unique phrases each ``task_*`` prompt emits in
:mod:`wolfbot.llm.prompt_builder`. The mock is used when
``GAMEPLAY_LLM_PROVIDER=mock`` so the full Master pipeline can run
end-to-end without burning real LLM tokens.
"""

from __future__ import annotations

from wolfbot.domain.enums import Role, SubmissionType
from wolfbot.llm.decider_config import LLMDeciderConfig
from wolfbot.llm.prompt_builder import (
    task_daytime_speech,
    task_night_action,
    task_vote,
    task_wolf_chat,
)
from wolfbot.services.llm_service import (
    LLMAction,
    MockLLMActionDecider,
    make_llm_decider,
)

CANDIDATES = ("席1 Alice", "席2 セツ", "席3 ジナ")


async def test_mock_decider_returns_vote_intent_with_null_target() -> None:
    decider = MockLLMActionDecider()
    user_ctx = task_vote(CANDIDATES, runoff=False)
    result = await decider.decide(system_prompt="", user_context=user_ctx)
    assert isinstance(result, LLMAction)
    assert result.intent == "vote"
    # null target → LLMAdapter._resolve_target falls back to random
    assert result.target_name is None


async def test_mock_decider_returns_night_action_with_smallest_seat() -> None:
    decider = MockLLMActionDecider()
    user_ctx = task_night_action(SubmissionType.WOLF_ATTACK, CANDIDATES)
    result = await decider.decide(system_prompt="", user_context=user_ctx)
    assert result.intent == "night_action"
    # Smallest 席N picked from the candidate list so both wolves converge
    # on the same target — otherwise mock attacks split and park the game
    # in WAITING_HOST_DECISION every night.
    assert result.target_name == "席1"


async def test_mock_decider_night_action_falls_back_to_none_when_no_seats() -> None:
    decider = MockLLMActionDecider()
    user_ctx = task_night_action(SubmissionType.WOLF_ATTACK, ())
    result = await decider.decide(system_prompt="", user_context=user_ctx)
    assert result.intent == "night_action"
    assert result.target_name is None


async def test_mock_decider_dispatches_when_task_text_is_in_system_prompt() -> None:
    """Regression: the real `_ask` path puts task_text into the *system*
    prompt (via `build_system_prompt`'s `{task_block}`). If the mock only
    checked user_context it would silently fall through to canned speech
    on every vote / night submission, target_name would default to None,
    and `_resolve_target` would log "LLM returned null target" and pick
    randomly — exactly the bug that broke wolf-attack convergence."""
    decider = MockLLMActionDecider()
    night_task = task_night_action(SubmissionType.WOLF_ATTACK, CANDIDATES)
    result = await decider.decide(system_prompt=night_task, user_context="user ctx")
    assert result.intent == "night_action"
    assert result.target_name == "席1"

    vote_task = task_vote(CANDIDATES, runoff=False)
    result = await decider.decide(system_prompt=vote_task, user_context="user ctx")
    assert result.intent == "vote"


async def test_mock_decider_returns_daytime_speech() -> None:
    decider = MockLLMActionDecider()
    user_ctx = task_daytime_speech(day_number=1, discussion_round=1, role=Role.VILLAGER)
    result = await decider.decide(system_prompt="", user_context=user_ctx)
    assert result.intent == "speak"
    assert result.public_message  # non-empty canned phrase


async def test_mock_decider_returns_wolf_chat_speech() -> None:
    decider = MockLLMActionDecider()
    user_ctx = task_wolf_chat(("席7 Bob",), CANDIDATES)
    result = await decider.decide(system_prompt="", user_context=user_ctx)
    assert result.intent == "speak"
    assert result.public_message


async def test_mock_decider_speech_round_robins_through_pool() -> None:
    decider = MockLLMActionDecider(speeches=("a", "b", "c"))
    user_ctx = task_daytime_speech(day_number=1, discussion_round=1, role=Role.VILLAGER)
    out = []
    for _ in range(5):
        r = await decider.decide(system_prompt="", user_context=user_ctx)
        out.append(r.public_message)
    assert out == ["a", "b", "c", "a", "b"]


def test_make_llm_decider_returns_mock_when_provider_is_mock() -> None:
    cfg = LLMDeciderConfig(provider="mock")
    decider = make_llm_decider(cfg)
    assert isinstance(decider, MockLLMActionDecider)
