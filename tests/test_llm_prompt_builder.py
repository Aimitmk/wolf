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
from wolfbot.llm.personas import PERSONAS_BY_KEY, Persona, SpeechProfile
from wolfbot.llm.prompt_builder import (
    _build_game_rules_block,
    _build_speech_profile_block,
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


def test_game_rules_block_contains_single_co_default_truthy_stance() -> None:
    """Shared rule: a lone CO with no counter-CO (never once in the public log)
    should be treated as likely real, and LLMs should not vote against them
    without grounds. `一度も` is the key anchor that distinguishes this case
    from a sole-survivor whose counter-CO was executed/attacked."""
    block = _build_game_rules_block()
    assert "単独 CO" in block
    assert "真の役職者にかなり近い" in block
    assert "対抗 CO" in block
    assert "一度も" in block


def test_game_rules_block_allows_single_co_suspicion_on_strong_evidence() -> None:
    """Single CO is not absolute truth — strong evidence (speech breakdown,
    vote contradiction, result contradiction, attack-pattern mismatch) can
    still justify suspicion."""
    block = _build_game_rules_block()
    assert "発言破綻" in block
    assert "投票矛盾" in block
    assert "判定結果の矛盾" in block
    assert "噛み筋" in block


def test_game_rules_block_guides_counter_co_comparison() -> None:
    """When a counter-CO appears, compare on results / timeline / votes /
    attack outcomes."""
    block = _build_game_rules_block()
    assert "対抗 CO" in block
    assert "時系列" in block
    assert "整合性" in block


def test_game_rules_block_rejects_sole_survivor_as_single_co() -> None:
    """If the same role had 2+ COs in the past, a currently-surviving lone CO
    holder is NOT treated as a lone/no-counter CO even though only one is
    alive. Prevents the LLM from auto-trusting a survivor whose opponent was
    executed or attacked."""
    block = _build_game_rules_block()
    assert "2 人以上" in block
    assert "自動的に真置きしない" in block


def test_game_rules_block_includes_dead_co_in_comparison() -> None:
    """For roles with a counter-CO history, dead CO holders stay in the
    comparison set — the LLM evaluates the survivor against the deceased
    opponent's results, speech, votes, attack outcomes AND death timing."""
    block = _build_game_rules_block()
    assert "死亡済み" in block
    assert "死亡タイミング" in block


def test_game_rules_block_explains_medium_white_means_not_wolf_only() -> None:
    """Medium white = `not a real werewolf`, not a role-claim confirmation.
    Every LLM seat must see this so that no role overreads a white result."""
    block = _build_game_rules_block()
    assert "人狼ではありませんでした" in block
    assert "本物の人狼ではない" in block
    assert "役職名" in block


def test_game_rules_block_protects_seer_co_from_medium_white_misread() -> None:
    """Medium-white on an executed Seer-CO does NOT invalidate the seer claim
    — a real seer who gets executed reads white too. The block must also
    state that suspicion of the seer CO requires non-medium-result grounds."""
    block = _build_game_rules_block()
    assert "占い師 CO" in block
    assert "真占い師だった可能性と矛盾しない" in block
    assert "偽扱いしない" in block


def test_game_rules_block_flags_seer_co_as_wolf_fake_on_medium_black() -> None:
    """The converse: medium-black on an executed Seer-CO is strong evidence
    the CO was a wolf fake, because only real werewolves read black."""
    block = _build_game_rules_block()
    assert "霊媒結果で黒" in block
    assert "本物の人狼" in block
    assert "人狼の騙り" in block


def test_game_rules_block_defines_3_1_formation() -> None:
    """3-1 = 3 seer COs + 1 medium CO. The shared rules must name this
    formation and note the 2-of-3 fake seer likelihood, treating the sole
    medium as a truth-leaning progression pivot."""
    block = _build_game_rules_block()
    assert "3-1" in block
    assert "占い師 CO が 3 人・霊媒師 CO が 1 人" in block
    assert "2 人が騙り" in block


def test_game_rules_block_names_seer_roller_and_black_stop() -> None:
    """3-1 gives two base-plan vocabulary items: seer roller and black stop.
    Both names must appear in the shared rules."""
    block = _build_game_rules_block()
    assert "占いローラー" in block
    assert "黒ストップ" in block


def test_game_rules_block_describes_seer_roller_procedure() -> None:
    """Seer roller hangs from fake-looking / wolf-looking / info-leaking seer
    COs, then cross-checks post-execution medium results against seer / vote
    / attack consistency."""
    block = _build_game_rules_block()
    assert "偽っぽい・狼っぽい・視点漏れ" in block
    assert "処刑後の霊媒結果" in block


def test_game_rules_block_describes_black_stop_and_its_limits() -> None:
    """Black stop (灰 scrutiny when sole medium reports black on a seer CO)
    is the alternative to continuing the roller, but it has explicit
    exceptions: 真狼狼, fake medium, breaking remaining seer CO, PP risk."""
    block = _build_game_rules_block()
    assert "灰 (役職 CO していない位置) の精査" in block
    assert "真狼狼" in block
    assert "PP (パワープレイ)" in block


def test_game_rules_block_defines_2_2_formation() -> None:
    """2-2 = 2 seer COs + 2 medium COs. Neither side is confirmed; medium
    roller (or 霊媒切り) is the default progression."""
    block = _build_game_rules_block()
    assert "2-2" in block
    assert "占い師 CO が 2 人・霊媒師 CO が 2 人" in block
    assert "霊媒ローラー" in block


def test_game_rules_block_requires_completing_medium_roller_by_default() -> None:
    """Two medium COs → don't unfoundedly trust either one; once a medium
    roller is started, complete it by default, with a high evidentiary bar
    to stop halfway."""
    block = _build_game_rules_block()
    assert "根拠なく真置きせず" in block
    assert "原則として完走" in block


# ------------------------------------- terminology (推理語彙) in rules block
# Advanced jinro vocabulary is shared across every LLM seat via the game-rules
# block (not per-role strategy). These assertions pin the substrings that the
# spec requires and guard against accidental leak of wolf-coordination
# vocabulary (`相方`, `襲撃先を揃える`) into the shared block.
def test_game_rules_block_frames_terminology_as_reading_tool() -> None:
    """The terminology section is introduced as a *reading* tool that does not
    override the preceding factual rules. The framing sentence anchors this."""
    block = _build_game_rules_block()
    assert "推理語彙" in block
    assert "最終判断は常に公開情報の整合性" in block


def test_game_rules_block_defines_grey_positions() -> None:
    """グレー / 灰 means: no role CO AND no settled seer/medium white-black on
    the seat. Both kanji and katakana forms must appear."""
    block = _build_game_rules_block()
    assert "グレー" in block
    assert "灰" in block
    assert "白黒も十分ついていない" in block


def test_game_rules_block_describes_guran_as_non_random() -> None:
    """グレラン is explicitly framed as reasoned grey-voting, not pure random —
    the spec calls out this misreading as the #1 failure mode."""
    block = _build_game_rules_block()
    assert "グレラン" in block
    assert "理由を持って" in block
    # The non-randomness must be made explicit; the word `無作為` appears only
    # in the negation phrase.
    assert "無作為" in block
    assert "完全な無作為投票ではなく" in block


def test_game_rules_block_defines_grey_scale_with_reasons() -> None:
    """グレスケ / スケール: not just an ordering — each position must carry a
    reason grounded in speech/vote/divination/attack consistency."""
    block = _build_game_rules_block()
    assert "グレスケ" in block
    assert "スケール" in block
    assert "白い順" in block
    assert "黒い順" in block
    assert "理由" in block


def test_game_rules_block_contains_rope_calculation_formula() -> None:
    """縄計算: remaining executions, with the standard heuristic formula
    spelled out. 9-player village starts at 4 縄."""
    block = _build_game_rules_block()
    assert "縄計算" in block
    assert "floor((生存人数 - 1) / 2)" in block
    assert "4縄" in block


def test_game_rules_block_clarifies_white_is_not_village_confirmed() -> None:
    """Every LLM seat must see the contract that 白判定 ≠ 村陣営確定 because
    the madman reads white. This also cross-references the existing rule on
    line ~55 (`狂人は黒判定されない`) — both coexist in the same block."""
    block = _build_game_rules_block()
    assert "狂人も白に出る" in block
    assert "村陣営確定ではない" in block
    # Guard: the pre-existing rule that the new terminology must not override.
    assert "狂人は黒判定されない" in block


def test_game_rules_block_defines_kakushiro_with_madman_caveat() -> None:
    """確白 = 進行役候補 but never absolute village-confirmation (狂人 reads
    white). The caveat must be phrased as `言い切りすぎない` to avoid promoting
    a 確白 to full 村陣営 status."""
    block = _build_game_rules_block()
    assert "確白" in block
    assert "進行役候補" in block
    assert "「村陣営確定」と言い切りすぎない" in block


def test_game_rules_block_rejects_single_fake_black_as_kakukuro() -> None:
    """確黒 requires multi-view corroboration. A single lone-black from a
    potentially-fake seer is NOT 確黒 — this must be stated verbatim so LLMs
    don't overweight single fake-seer blacks during 2-2 / counter-CO."""
    block = _build_game_rules_block()
    assert "確黒" in block
    assert "単独の偽占い候補から黒を出されただけでは確黒ではない" in block


def test_game_rules_block_defines_panda_as_both_white_and_black() -> None:
    """パンダ = a seat that received BOTH white and black judgments (from
    different COs). The phrase `白判定と黒判定の両方` is the canonical test
    anchor."""
    block = _build_game_rules_block()
    assert "パンダ" in block
    assert "白判定と黒判定の両方" in block


def test_game_rules_block_defines_roller_synonyms_with_completion() -> None:
    """Both spellings ローラー and ロラ must appear as common vocabulary, and
    the completion rule must be restated here so it cross-references (not
    contradicts) the existing 2-2 `原則として完走` rule."""
    block = _build_game_rules_block()
    assert "ローラー" in block
    assert "ロラ" in block
    assert "開始したら原則完走" in block


def test_game_rules_block_defines_kimeuchi_and_hatan() -> None:
    """決め打ち and 破綻 must both appear as first-class terminology bullets,
    not just as word-in-sentence uses elsewhere in the block."""
    block = _build_game_rules_block()
    assert "- 決め打ち:" in block
    assert "- 破綻:" in block


def test_game_rules_block_defines_line_kakoi_minuchigiri() -> None:
    """Wolf-pattern vocabulary (ライン / 囲い / 身内切り) reaches every seat as
    neutral reading tools, framed as patterns to *recognize*, not execute."""
    block = _build_game_rules_block()
    assert "- ライン:" in block
    assert "- 囲い:" in block
    assert "- 身内切り:" in block


def test_game_rules_block_defines_vote_and_attack_traces_and_shiten() -> None:
    """Behavioral-signal vocabulary: 票筋 / 噛み筋 / 視点漏れ must each have a
    dedicated bullet defining the term, not just appear mid-sentence."""
    block = _build_game_rules_block()
    assert "- 票筋:" in block
    assert "- 噛み筋:" in block
    assert "- 視点漏れ:" in block


def test_game_rules_block_defines_endgame_vocabulary() -> None:
    """Endgame terms: SG (scapegoat), GJ / 平和 (peaceful morning), PP
    (power-play), RPP (random / lost PP) must all be defined so LLMs can
    recognize and reason about late-game vote dynamics."""
    block = _build_game_rules_block()
    assert "- SG" in block
    assert "- GJ" in block
    assert "平和" in block
    assert "- PP" in block
    assert "- RPP" in block


def test_game_rules_block_terminology_has_no_wolf_coordination_leak() -> None:
    """Shared terminology must not bleed wolf-coordination vocabulary into
    non-wolf prompts. `相方` and the exact phrase `襲撃先を揃える` are the two
    anchors the existing service-level leak tests use."""
    block = _build_game_rules_block()
    assert "相方" not in block, (
        "wolf-coordination '相方' leaked into shared rules block — would break "
        "test_ask_system_prompt_non_wolf_excludes_wolf_strategy"
    )
    assert "襲撃先を揃える" not in block, (
        "wolf-coordination '襲撃先を揃える' leaked into shared rules block"
    )


# ------------------------------------------------------- strategy block
# A phrase that must appear in exactly one role's tips — keyed by role. Used
# both to assert per-role content AND to assert no cross-leak into other roles.
_ROLE_UNIQUE_PHRASES: dict[Role, str] = {
    Role.WEREWOLF: "相方を露骨に庇いすぎない",
    Role.MADMAN: "人狼位置を知っている前提で話してはならない",
    Role.SEER: "判定履歴を時系列で一貫",
    Role.MEDIUM: "処刑された相手が狂人でも",
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


def test_medium_strategy_reinforces_madman_is_white_rule() -> None:
    block = _build_strategy_block(Role.MEDIUM)
    assert "人狼ではありませんでした" in block
    assert "白結果だけでは村置き確定にはならない" in block


def test_medium_strategy_guards_against_seer_co_white_misread() -> None:
    """The Medium must be told explicitly that medium-white on an executed
    Seer-CO is not proof of a fake seer — the role-specific analog of the
    shared rule in `_build_game_rules_block`."""
    block = _build_strategy_block(Role.MEDIUM)
    assert "占い師 CO" in block
    assert "霊媒結果が白" in block
    assert "占い師 CO 偽の証明ではない" in block


def test_medium_strategy_separates_real_seer_from_non_wolf_fake() -> None:
    """When medium-white lands on a Seer-CO, the medium should partition the
    hypothesis space: real seer vs. non-wolf fake (madman, etc.)."""
    block = _build_strategy_block(Role.MEDIUM)
    assert "真占い師だった可能性" in block
    assert "狂人" in block
    assert "非狼" in block


def test_medium_strategy_routes_seer_co_suspicion_through_corroboration() -> None:
    """To suspect a Seer-CO, medium must cite non-medium-result evidence:
    counter CO, divination breakdown, speech timeline, votes, attack result,
    death timing — NOT the white medium result itself."""
    block = _build_strategy_block(Role.MEDIUM)
    assert "対抗 CO" in block
    assert "発言時系列" in block
    assert "襲撃結果" in block
    assert "死亡タイミング" in block


def test_knight_strategy_advises_protection_success_co() -> None:
    """On a peaceful morning (no casualty), the knight should consider CO-ing
    with the guard target attached — and must always attach the guard target
    when CO-ing on a protection-success claim."""
    block = _build_strategy_block(Role.KNIGHT)
    assert "平和な朝" in block
    assert "護衛成功" in block
    assert "護衛先を添えて" in block
    # Existing guidance is preserved.
    assert "前夜と違う相手を選ぶ" in block


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
def test_fake_strategy_prioritizes_seer_fake_on_day1(role: Role) -> None:
    """Wolf and madman both get guidance to consider faking seer on day 1."""
    block = _build_strategy_block(role)
    assert "day 1" in block
    assert "占い師騙り" in block


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
def test_fake_strategy_switches_to_medium_or_knight_fake_if_countered(role: Role) -> None:
    """If a counter-seer CO is already out, both wolf and madman should
    consider medium or knight fake on day 2+, with corresponding night-ability
    results attached."""
    block = _build_strategy_block(role)
    assert "対抗占い師" in block
    assert "day 2" in block
    assert "霊媒師騙り" in block
    assert "騎士騙り" in block
    assert "夜に能力を使った想定" in block


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
def test_fake_strategy_warns_against_over_faking(role: Role) -> None:
    """Both wolf and madman are warned that piling on fake COs (3+) makes
    non-CO seats look white by contrast."""
    block = _build_strategy_block(role)
    assert "3 人以上" in block
    assert "騙りすぎ" in block


def test_madman_fake_strategy_has_no_wolf_coordination_vocabulary() -> None:
    """The madman's new fake-CO guidance must not introduce wolf-coordination
    vocabulary (`相方`, `襲撃先を揃える`) — the madman does not know the real
    wolf positions."""
    block = _build_strategy_block(Role.MADMAN)
    assert "相方" not in block
    assert "襲撃先を揃える" not in block
    # Existing prohibition phrase still present.
    assert "人狼位置を知っている前提で話してはならない" in block


# --------------------------------------------------- build_system_prompt
# Sentinels (not real pronouns) so the test persona can't false-positive in
# other tests that grep for `私` / `君` / etc. in the rendered prompt.
_TEST_SPEECH_PROFILE = SpeechProfile(
    first_person="TEST_FP",
    address_style="TEST_ADDRESS",
    sentence_style="TEST_SENTENCE",
    pause_style="TEST_PAUSE",
)
_TEST_PERSONA = Persona(
    key="_test_persona_",
    display_name="TEST_NAME",
    style_guide="TEST_STYLE_GUIDE",
    speech_profile=_TEST_SPEECH_PROFILE,
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
    # speech profile block reached (sentinels in _TEST_SPEECH_PROFILE)
    assert "TEST_ADDRESS" in prompt
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


# --------------------------------------------------- speech profile block
# Structural assertions. Content (specific phrases per persona) is sourced from
# the spec file; these tests pin the shape + a few high-signal anchors so
# regressions in the renderer are caught early.
def test_speech_profile_block_standard_persona_lists_first_person() -> None:
    block = _build_speech_profile_block(PERSONAS_BY_KEY["setsu"])
    assert "一人称" in block
    assert "『私』" in block
    assert "君" in block
    # Standard mode must not bleed silent_gesture markers.
    assert "叙述モード" not in block


def test_speech_profile_block_sq_lists_self_alias_and_low_frequency_flag() -> None:
    block = _build_speech_profile_block(PERSONAS_BY_KEY["sq"])
    assert "『アタシ』" in block
    assert "SQちゃん" in block
    assert "DEATH" in block
    # Rate-limiting advice must appear so the LLM is told DEATH is sparing.
    assert "低頻度" in block


def test_speech_profile_block_yuriko_uses_konomi_and_not_kimi() -> None:
    """Yuriko's 2nd person is『お前』, not『君』— a common LLM drift hazard."""
    block = _build_speech_profile_block(PERSONAS_BY_KEY["yuriko"])
    assert "『この身』" in block
    assert "お前" in block
    assert "君" not in block


def test_speech_profile_block_kukrushka_is_silent_gesture() -> None:
    """Kukrushka's block is structurally different (no `一人称` line, has
    `叙述モード` + gesture examples instead)."""
    block = _build_speech_profile_block(PERSONAS_BY_KEY["kukrushka"])
    assert "叙述モード" in block
    assert "所作" in block
    # Structural difference: the normal `一人称` line is absent.
    assert "一人称" not in block


@pytest.mark.parametrize(
    "key",
    [k for k, p in PERSONAS_BY_KEY.items() if p.speech_profile.narration_mode == "standard"],
)
def test_speech_profile_block_standard_personas_share_structure(key: str) -> None:
    """All `narration_mode == "standard"` personas share the same scaffold.
    Filtering by attribute (not by hard-coded key exclusion) keeps the test
    correct if another silent-gesture persona is added later."""
    block = _build_speech_profile_block(PERSONAS_BY_KEY[key])
    assert "一人称" in block
    assert "叙述モード" not in block


def test_build_system_prompt_includes_speech_profile_block() -> None:
    prompt = build_system_prompt(
        persona=PERSONAS_BY_KEY["setsu"],
        role=Role.VILLAGER,
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        task_text="t",
    )
    assert "## 話法" in prompt
    assert "『私』" in prompt
    # Common rule from the markdown template (static, once).
    assert "1 発話に入れてよい特徴語は多くても 1 個" in prompt


def test_build_system_prompt_speech_profile_respects_persona() -> None:
    """The `## 話法` section differs per persona — setsu's speech block says
    『私』, yuriko's says『この身』. Neither block leaks the other's first
    person."""
    setsu_prompt = build_system_prompt(
        persona=PERSONAS_BY_KEY["setsu"],
        role=Role.VILLAGER,
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        task_text="t",
    )
    yuriko_prompt = build_system_prompt(
        persona=PERSONAS_BY_KEY["yuriko"],
        role=Role.VILLAGER,
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        task_text="t",
    )
    assert "『私』" in setsu_prompt
    assert "『この身』" not in setsu_prompt
    assert "『この身』" in yuriko_prompt
    # Everything after `## 話法` (speech block + later sections) must not
    # contain『私』 for yuriko — guards against drift in rendering.
    assert "『私』" not in yuriko_prompt.split("## 話法")[1]
