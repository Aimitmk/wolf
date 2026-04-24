"""Tests for `prompt_builder._build_game_rules_block`, `_build_strategy_block`,
and `build_system_prompt`.

These pin down two invariants:
- The game-rules block (common to every LLM seat) states the fixed 9-player
  ruleset, role distribution, win conditions, and the non-obvious mechanics
  (NIGHT_0 random white, madman-is-white, wolf-split failure, knight no
  consecutive guard, candidate-token strict match).
- The role-strategy block is role-scoped: each role's tips are not visible to
  any other role. In particular, wolf-coordination vocabulary (`相方`,
  `襲撃先を揃える`) appears only in the werewolf tips; the madman's tips
  prohibit (not assume) knowing real wolf positions.
"""

from __future__ import annotations

import re

import pytest

from wolfbot.domain.enums import ROLE_JA, Phase, Role
from wolfbot.llm.personas import Persona
from wolfbot.llm.prompt_builder import (
    _build_game_rules_block,
    _build_strategy_block,
    build_system_prompt,
)


# --------------------------------------------------------- game rules block
def test_game_rules_block_contains_role_distribution() -> None:
    block = _build_game_rules_block()
    for role_ja, count in (
        ("人狼", 2),
        ("狂人", 1),
        ("占い師", 1),
        ("霊媒師", 1),
        ("騎士", 1),
        ("村人", 3),
    ):
        assert f"{role_ja}{count}" in block, f"{role_ja}{count} missing from rules block"


def test_game_rules_block_states_nine_player_village() -> None:
    block = _build_game_rules_block()
    assert "9 人村" in block


def test_game_rules_block_contains_win_conditions() -> None:
    block = _build_game_rules_block()
    # Must mirror rules.check_victory exactly.
    assert "生存人狼数が 0" in block
    assert "生存人狼数が生存非人狼人数以上" in block


def test_game_rules_block_contains_night0_random_white_rule() -> None:
    block = _build_game_rules_block()
    assert "NIGHT_0" in block
    assert "ランダム白" in block
    assert "本物の人狼ではない" in block


def test_game_rules_block_contains_seer_medium_wolf_only_rule() -> None:
    block = _build_game_rules_block()
    assert "本物の人狼だけを黒" in block
    assert "狂人は黒判定されない" in block


def test_game_rules_block_contains_wolf_split_attack_rule() -> None:
    block = _build_game_rules_block()
    assert "意見が割れる" in block
    assert "空振り" in block


def test_game_rules_block_contains_knight_consecutive_guard_rule() -> None:
    block = _build_game_rules_block()
    assert "連続で護衛" in block
    assert "前夜と同じ対象" in block


def test_game_rules_block_contains_candidate_token_rule() -> None:
    block = _build_game_rules_block()
    assert "候補トークン" in block


# ------------------------------------------------------- strategy block
# A phrase that must appear in exactly one role's tips — keyed by role. Used
# both to assert per-role content AND to assert no cross-leak into other roles.
_ROLE_UNIQUE_PHRASES: dict[Role, str] = {
    Role.WEREWOLF: "相方を露骨に庇いすぎない",
    Role.MADMAN: "人狼位置を知っている前提で話してはならない",
    Role.SEER: "判定履歴を時系列で一貫",
    Role.MEDIUM: "対抗霊媒が出た場合",
    Role.KNIGHT: "前夜と違う相手を選ぶ",
    Role.VILLAGER: "CO 騙りは村陣営としては行わない",
}


@pytest.mark.parametrize(("role", "phrase"), list(_ROLE_UNIQUE_PHRASES.items()))
def test_strategy_block_for_each_role_contains_own_tips(role: Role, phrase: str) -> None:
    block = _build_strategy_block(role)
    assert phrase in block, f"{role.name}'s strategy missing its own phrase {phrase!r}"


@pytest.mark.parametrize("role", list(Role))
def test_strategy_block_no_cross_role_leak(role: Role) -> None:
    """For a given role, none of the OTHER roles' unique phrases may appear."""
    block = _build_strategy_block(role)
    for other_role, other_phrase in _ROLE_UNIQUE_PHRASES.items():
        if other_role is role:
            continue
        assert other_phrase not in block, (
            f"{other_role.name}'s tip leaked into {role.name}'s strategy block"
        )


@pytest.mark.parametrize("role", list(Role))
def test_wolf_coordination_vocabulary_only_in_wolf_strategy(role: Role) -> None:
    """`相方` and `襲撃先を揃える` are wolf-playbook vocabulary and must only
    appear in the werewolf strategy block — never in any other role's tips."""
    block = _build_strategy_block(role)
    if role is Role.WEREWOLF:
        assert "相方" in block
        assert "襲撃先を揃える" in block
    else:
        assert "相方" not in block, f"wolf coordination '相方' leaked into {role.name}"
        assert "襲撃先を揃える" not in block, (
            f"wolf coordination '襲撃先を揃える' leaked into {role.name}"
        )


def test_madman_strategy_prohibits_not_assumes_wolf_positions() -> None:
    """The madman must NOT be told that wolf positions are known — only that
    the opposite is true. The strategy text phrases this as a prohibition, so
    the full prohibition phrase must be present and wolf-coordination tips
    must still be absent (the madman does not get the wolves' playbook)."""
    block = _build_strategy_block(Role.MADMAN)
    assert "人狼位置を知っている前提で話してはならない" in block
    # No wolf-coordination playbook leaks.
    assert "相方" not in block
    assert "襲撃先を揃える" not in block


# --------------------------------------------------- build_system_prompt
_TEST_PERSONA = Persona(
    key="_test_persona_",
    display_name="TEST_NAME",
    style_guide="TEST_STYLE_GUIDE",
)


def test_existing_blocks_still_rendered() -> None:
    """persona_block / role_block / phase_block / task_block substitutions
    still happen after the rules+strategy additions."""
    prompt = build_system_prompt(
        persona=_TEST_PERSONA,
        role=Role.VILLAGER,
        phase=Phase.DAY_DISCUSSION,
        day_number=2,
        task_text="TASK_BLOCK_MARKER_XYZ",
    )
    # persona
    assert "TEST_NAME" in prompt
    assert "TEST_STYLE_GUIDE" in prompt
    # role (via ROLE_JA)
    assert ROLE_JA[Role.VILLAGER] in prompt
    # phase
    assert Phase.DAY_DISCUSSION.value in prompt
    assert "day 2" in prompt
    # task
    assert "TASK_BLOCK_MARKER_XYZ" in prompt


@pytest.mark.parametrize("role", list(Role))
def test_build_system_prompt_has_no_unreplaced_placeholders(role: Role) -> None:
    """Every `{placeholder}` token in the template must be substituted for
    every role. A leftover `{role_block}` etc. would be a bug."""
    prompt = build_system_prompt(
        persona=_TEST_PERSONA,
        role=role,
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        task_text="t",
    )
    leftover = re.findall(r"\{[a-z_]+_block\}", prompt)
    assert leftover == [], f"unreplaced placeholders for {role.name}: {leftover}"


@pytest.mark.parametrize("role", list(Role))
def test_build_system_prompt_embeds_role_appropriate_strategy(role: Role) -> None:
    """The full system prompt for a given role must contain that role's own
    unique strategy phrase AND must NOT contain any other role's unique
    strategy phrase. This is the integration-level analog of
    `test_strategy_block_no_cross_role_leak`."""
    prompt = build_system_prompt(
        persona=_TEST_PERSONA,
        role=role,
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        task_text="t",
    )
    assert _ROLE_UNIQUE_PHRASES[role] in prompt
    for other_role, other_phrase in _ROLE_UNIQUE_PHRASES.items():
        if other_role is role:
            continue
        assert other_phrase not in prompt, (
            f"{other_role.name}'s tip leaked into full system prompt for {role.name}"
        )


def test_build_system_prompt_contains_common_rules_for_any_role() -> None:
    """The rules block is shared across all roles — every role's system prompt
    must contain the canonical rule markers."""
    for role in Role:
        prompt = build_system_prompt(
            persona=_TEST_PERSONA,
            role=role,
            phase=Phase.DAY_DISCUSSION,
            day_number=1,
            task_text="t",
        )
        # Role distribution markers (sampled) — derived from ROLE_DISTRIBUTION.
        assert "人狼2" in prompt
        assert "村人3" in prompt
        # Win conditions — mirrors rules.check_victory.
        assert "生存人狼数が 0" in prompt
        assert "生存人狼数が生存非人狼人数以上" in prompt
