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

from wolfbot.domain.enums import ROLE_JA, Phase, Role, SubmissionType
from wolfbot.domain.models import Game, Player, Seat
from wolfbot.llm.personas import PERSONAS_BY_KEY, Persona, SpeechProfile
from wolfbot.llm.prompt_builder import (
    _build_game_rules_block,
    _build_speech_profile_block,
    _build_strategy_block,
    build_system_prompt,
    build_user_context,
    task_daytime_speech,
    task_night_action,
    task_vote,
    task_wolf_chat,
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
    assert "意見が割れた場合" in block
    assert "空振り" in block


def test_game_rules_block_documents_human_wolf_priority_exception() -> None:
    """Domain has human-wolf priority for mixed (1 human + 1 LLM) splits.

    LLM wolves must understand the asymmetry — without it they assume any
    split fails and may misjudge attack-target choice when paired with a human.
    """
    block = _build_game_rules_block()
    assert "人間プレイヤー" in block
    assert "LLM 席" in block
    assert "採用" in block


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


def test_game_rules_block_warns_against_treating_last_surviving_co_as_truth() -> None:
    """When a counter-CO history exists and only one CO is left alive, the LLM
    must not short-circuit to "lone CO ⇒ truth". The wording must explicitly
    name the wolf-side mechanisms that produce a sole survivor (not attacking
    the info role, getting a counter-CO executed, leaving them for protective
    cover) and forbid the "single CO so truth" shortcut."""
    block = _build_game_rules_block()
    assert "最後まで生き残った" in block
    assert "噛まずに残した" in block
    assert "単独 CO だから真" in block


def test_game_rules_block_co_recognition_requires_explicit_self_declaration() -> None:
    """Topical mentions / hypotheticals / references to others using CO
    vocabulary (`占いCO が出たら` etc.) must NOT be read as the speaker's own
    CO. The rules block must explicitly carry the self-declaration policy and
    the example phrases for each side (topical vs. self-declaration)."""
    block = _build_game_rules_block()
    # Topical phrases the LLM must NOT misread as self-CO.
    assert "占いCOが出たら" in block
    assert "霊媒COについて" in block
    # Self-declaration example phrases.
    assert "私は占い師です" in block
    assert "占い師COします" in block
    assert "霊媒師として出ます" in block
    # Policy framing.
    assert "話題化" in block
    assert "名乗り" in block
    assert "判断に迷うときは CO として数えない" in block


def test_game_rules_block_co_recognition_no_wolf_coordination_leak() -> None:
    """Defensive duplicate of the existing terminology leak-test focused on the
    new CO-recognition bullets — guarantees the new policy text does not bleed
    wolf-coordination vocabulary into the shared rules block. Bare `相方`
    (actor mode, partner-known) is forbidden; `相方候補` (public-log inference)
    is allowed in the 2-wolf-pair-inference subsection."""
    block = _build_game_rules_block()
    assert not re.search(r"相方(?!候補)", block), (
        "bare '相方' (actor mode) leaked into shared rules block"
    )
    assert "襲撃先を揃える" not in block


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


def test_game_rules_block_defines_2_1_formation() -> None:
    """2-1 = 2 seer COs + 1 medium CO. Sole medium reads as truth-leaning
    pivot; branch on black-out vs white-progress with rope / 囲い awareness."""
    block = _build_game_rules_block()
    assert "2-1" in block
    assert "占い師 CO が 2 人・霊媒師 CO が 1 人" in block
    assert "単独霊媒師" in block
    assert "グレー吊り" in block
    assert "黒吊り" in block


def test_game_rules_block_defines_1_2_formation() -> None:
    """1-2 = 1 seer CO + 2 medium COs. Seer reads truth-leaning while mediums
    are treated as mixed-fake; medium roller / medium-kiri is default, grey
    scrutiny only when medium internals read real+madman and grey is
    wolf-heavy."""
    block = _build_game_rules_block()
    assert "1-2" in block
    assert "占い師 CO が 1 人・霊媒師 CO が 2 人" in block
    assert "霊媒ローラー" in block
    assert "霊媒切り" in block


def test_game_rules_block_requires_completing_medium_roller_by_default() -> None:
    """Two medium COs → don't unfoundedly trust either one; once a medium
    roller is started, complete it by default, with a high evidentiary bar
    to stop halfway."""
    block = _build_game_rules_block()
    assert "根拠なく真置きせず" in block
    assert "原則として完走" in block


# -------------------------- 3 占い CO + 2 非狼確定 で残る 1 人を確定黒級とする消去法
def test_game_rules_block_describes_three_seer_co_elimination_inference() -> None:
    """3-1 で占い CO 3 人のうち 2 人が公開情報上『本物の人狼ではない』と確定したら、
    残る 1 人を固定配役上の消去法で確定黒級の人狼位置として扱う。"""
    block = _build_game_rules_block()
    assert "2 人が公開情報上『本物の人狼ではない』と確定" in block
    assert "残る 1 人の占い師 CO を固定配役上の消去法" in block
    assert "確定黒級" in block
    assert "人狼 2 人固定の配役" in block


def test_game_rules_block_distinguishes_white_judgement_from_non_wolf_confirmation() -> None:
    """『白判定』と『非狼確定』を混同しない。信用未確定の占い CO の白、
    偽が混じり得る霊媒結果、印象だけの白寄り評価は非狼確定として数えない。"""
    block = _build_game_rules_block()
    assert "『白判定』と『非狼確定』を混同しない" in block
    assert "信用が未確定な占い師 CO が出した白" in block
    assert "偽が混じり得る霊媒結果" in block
    assert "印象だけの白寄り評価は非狼確定として数えない" in block
    assert "単発の白だけで非狼扱いを固定しない" in block


def test_game_rules_block_lists_acceptable_non_wolf_confirmation_grounds() -> None:
    """非狼確定として数えてよい根拠は公開ログ・霊媒結果・襲撃死・
    真寄り情報役の判定・CO 破綻整理に限る。"""
    block = _build_game_rules_block()
    assert "公開ログ・霊媒結果・襲撃死・真寄り情報役の判定" in block
    assert "CO 破綻整理" in block
    assert "本物の人狼ではないと示す襲撃死" in block


def test_game_rules_block_treats_remaining_seer_co_as_fixed_wolf_position() -> None:
    """2 人非狼確定が成立したら、残る占い師 CO は『まだ灰の 1 人』ではなく
    固定配役上の狼位置として投票・発言・進行提案へ反映させる。"""
    block = _build_game_rules_block()
    assert "『まだ灰の 1 人』ではなく" in block
    assert "固定配役上の狼位置として投票・発言・進行提案へ反映" in block
    assert "残る占い師 CO の処刑提案" in block


def test_game_rules_block_releases_fixed_black_when_premise_collapses() -> None:
    """村陣営騙り・CO 撤回・霊媒師 CO 偽の浮上・非狼確定根拠の破綻など
    前提が崩れたら、確定黒扱いを解除して時系列から再整理する。"""
    block = _build_game_rules_block()
    assert "前提が崩れた場合は確定黒扱いを解除して時系列から再整理" in block
    assert "村陣営の騙り" in block
    assert "CO 撤回" in block
    # 既存の line 108 文言と区別するため『の浮上』『後から』語尾の anchor を使う
    assert "霊媒師 CO 側が偽だった可能性が後から浮上した状況" in block
    assert "非狼確定の根拠が後から破綻した場合" in block
    assert "CO 履歴と判定履歴を時系列で再整理" in block


# ----------- 3 役職横断: CO 数・対抗 CO 超過分から非 CO 確白を読む消去法
# 占い師・霊媒師・騎士の各 CO 数を時系列で整理し、各役職について `CO 数 - 1`
# を「対抗 CO 超過分」(騙り最低数) として数える。3 役職分の超過分を合計して 3 に
# 達した場合、人狼 2 + 狂人 1 の狼陣営 3 名が能力役職 CO 群に出切っているため、
# 能力役職 CO していない位置は配役上の消去法で村陣営の確白級として扱える。
def test_game_rules_block_defines_co_overflow_term() -> None:
    """占い師・霊媒師・騎士の各 CO 数を時系列で整理し、CO 数 - 1 を対抗 CO 超過分
    (騙り最低数) として数える、という用語と式が共通ルールに入っていること。"""
    block = _build_game_rules_block()
    assert "対抗 CO 超過分" in block
    assert "CO 数 - 1" in block
    assert "騙り最低数" in block
    assert "真役職は各 1 人だけ" in block


def test_game_rules_block_overflow_sum_three_marks_non_co_as_village_white() -> None:
    """超過分合計が 3 に達した場合、能力役職 CO していない位置を村陣営の確白級
    として扱う。配役上の消去法で狼陣営 3 名が CO 群に出切ったと数える。"""
    block = _build_game_rules_block()
    assert "超過分合計が 3 に達した場合" in block
    assert "人狼 2 + 狂人 1" in block
    assert "能力役職 CO していない位置" in block
    assert "村陣営の確白級" in block
    assert "配役上の消去法" in block


def test_game_rules_block_overflow_sum_three_promotes_independent_single_co() -> None:
    """超過分合計 3 のとき、対抗のない単独 CO 役職が別にあれば、
    その単独 CO 者も狼陣営ではないため真役職としてかなり強く扱える。"""
    block = _build_game_rules_block()
    assert "対抗のない単独 CO 役職" in block
    assert "狼陣営ではないため真役職としてかなり強く扱える" in block


def test_game_rules_block_distinguishes_overflow_inference_from_madman_white() -> None:
    """超過分合計 3 による非 CO 確白は、単発の白判定 (狂人白) とは別根拠で、
    固定配役上の消去法で狼陣営 3 名が出切ったと数える点で村陣営まで強く推せる。"""
    block = _build_game_rules_block()
    assert "単発の白判定" in block
    # 既存の確白語彙ルール (狂人白との整合) と矛盾しない言い回し。
    assert "固定配役上の消去法で狼陣営 3 名が CO 群に出切った" in block
    assert "村陣営まで強く推せる" in block


def test_game_rules_block_overflow_sum_three_within_group_truth_still_needs_signals() -> None:
    """対抗 CO 群の中で誰が真役職かまでは、超過分合計 3 だけでは決まらない。
    判定結果・霊媒結果・投票・襲撃・死亡タイミング・破綻で詰める。"""
    block = _build_game_rules_block()
    assert "対抗 CO 群の中で誰が真役職かまでは超過分合計だけでは特定できない" in block
    # 詰めに使う材料: 4 軸以上の言及。
    assert "判定結果・霊媒結果・投票・襲撃" in block
    assert "死亡タイミング" in block
    assert "破綻" in block


def test_game_rules_block_overflow_sum_low_does_not_confirm_non_co() -> None:
    """超過分合計が 0〜2 の段階では、狼陣営が非 CO や単独 CO に残っている
    可能性があるため、非 CO 位置を CO 数だけで確白とは断定しない。"""
    block = _build_game_rules_block()
    assert "0〜2" in block
    assert "断定しない" in block
    assert "狼陣営が非 CO や単独 CO に残っている可能性" in block


def test_game_rules_block_overflow_sum_high_triggers_recheck() -> None:
    """超過分合計が 4 以上に見える場合は固定配役と矛盾する。CO 撤回・
    同一人物の複数 CO・話題としての CO 語彙の誤読・村騙り・死亡済み CO
    見落とし・ログ見落としを疑い、確白扱いを保留して時系列を再整理する。"""
    block = _build_game_rules_block()
    assert "4 以上" in block
    assert "固定配役と矛盾" in block
    assert "CO 撤回" in block
    assert "同一人物の複数 CO" in block
    assert "村騙り" in block
    assert "死亡済み CO 見落とし" in block
    assert "確白扱いを保留して時系列を再整理する" in block


def test_game_rules_block_includes_co_overflow_examples() -> None:
    """LLM が数え方を誤らないよう、3-2-1 / 2-2-2 / 3-1-1 / 4-1-1 の
    短い例が共通ルールに入っていること。"""
    block = _build_game_rules_block()
    # 数え方の型: 超過分合計 3 / 2 の境界、4-1-1 のような偏ったケース。
    assert "3-2-1" in block
    assert "2-2-2" in block
    assert "3-1-1" in block
    assert "4-1-1" in block
    # 計算式が例の中に明示されているか (LLM が暗算を間違えない補助)。
    assert "2 + 1 + 0 = 3" in block
    assert "1 + 1 + 1 = 3" in block
    assert "2 + 0 + 0 = 2" in block
    assert "3 + 0 + 0 = 3" in block


def test_game_rules_block_co_overflow_no_wolf_coordination_leak() -> None:
    """新規追加した CO 超過分推理ブロックが、wolf-coordination 語彙
    (bare `相方`, `襲撃先を揃える`) を共通ルールに漏らさないこと。
    `相方候補` (公開ログからの推理用語) は許容。"""
    block = _build_game_rules_block()
    assert not re.search(r"相方(?!候補)", block), (
        "bare '相方' (actor mode) leaked into shared rules block"
    )
    assert "襲撃先を揃える" not in block


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


def test_game_rules_block_kakoi_does_not_treat_madman_as_known_ally() -> None:
    """囲い must not describe the madman as a wolf-known ally. The old phrasing
    `仲間の狼 (や狂人)` implied that wolves know the madman's seat, which is
    false in this bot."""
    block = _build_game_rules_block()
    assert "仲間の狼 (や狂人)" not in block
    assert "狼は狂人位置を知らない" in block


def test_game_rules_block_minuchigiri_does_not_treat_madman_as_known_ally() -> None:
    """身内切り must not phrase the madman as a known wolf ally to cut. The
    old phrasing `仲間 (別の人狼や狂人)` incorrectly included 狂人 as a known
    teammate."""
    block = _build_game_rules_block()
    assert "仲間 (別の人狼や狂人)" not in block
    assert "狼が仲間の狼" in block


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


def test_game_rules_block_defines_advanced_guard_vocabulary() -> None:
    """Advanced jinrō guard vocabulary reaches every seat via the shared
    rules block so that public-log mentions of 鉄板護衛 / 変態護衛 / 捨て護衛
    / 護衛読み / 護衛誘導 / 連ガ無し / 狩人 are interpretable. Each term gets
    its own dedicated bullet."""
    block = _build_game_rules_block()
    assert "- 鉄板護衛:" in block
    assert "- 変態護衛:" in block
    assert "- 捨て護衛:" in block
    assert "- 護衛読み:" in block
    assert "- 護衛誘導:" in block
    # 連ガ無し / 連続護衛不可 vocabulary bullet.
    assert "- 連ガ無し" in block
    assert "連続護衛不可" in block
    # 狩人 / 狩 synonym mapping to 騎士.
    assert "狩人" in block
    assert "騎士と同じ意味" in block


def test_game_rules_block_clarifies_sutekogo_is_legal_target_choice_not_skip() -> None:
    """The 捨て護衛 vocabulary bullet must explicitly say it is a
    legal-candidate selection in this bot, not a skip / unsubmitted /
    no-target action — otherwise an LLM might map 捨て護衛 to `intent=skip`."""
    block = _build_game_rules_block()
    assert "捨て護衛" in block
    assert "合法護衛候補" in block
    assert "1 名を選ぶ行動" in block
    # Negation phrasing — captures all three forbidden interpretations.
    assert "未提出" in block
    assert "skip ではない" in block


def test_game_rules_block_advanced_guard_vocab_no_wolf_coordination_leak() -> None:
    """Defensive duplicate of the existing leak guard, focused on the new
    advanced-guard bullets — they must not bleed wolf-coordination vocab into
    the shared rules block. Bare `相方` (actor mode, partner-known) must be
    absent; the inference noun `相方候補` (candidate) is allowed."""
    block = _build_game_rules_block()
    assert not re.search(r"相方(?!候補)", block), (
        "bare '相方' (actor mode) leaked into shared rules block"
    )
    assert "襲撃先を揃える" not in block


def test_game_rules_block_terminology_has_no_wolf_coordination_leak() -> None:
    """Shared terminology must not bleed wolf-coordination vocabulary into
    non-wolf prompts. Bare `相方` (actor mode, partner-known) and the exact
    phrase `襲撃先を揃える` are the two anchors. The inference noun `相方候補`
    is allowed in the shared 2-wolf-pair-inference subsection."""
    block = _build_game_rules_block()
    assert not re.search(r"相方(?!候補)", block), (
        "bare '相方' (actor mode) leaked into shared rules block — would break "
        "test_ask_system_prompt_non_wolf_excludes_wolf_strategy"
    )
    assert "襲撃先を揃える" not in block, (
        "wolf-coordination '襲撃先を揃える' leaked into shared rules block"
    )


def test_game_rules_block_states_fake_co_legality() -> None:
    """Fake COs (seer/medium/knight) must stay within the bot's legal mechanics.
    Shared rules call out illegal knight-guard patterns (self-guard, consecutive
    guard, dead-seat guard) and forbid fabricating medium results on no-execution
    days or seer results that contradict the public timeline."""
    block = _build_game_rules_block()
    assert "実ルール上あり得る内容" in block
    assert "過去に自分が出した結果と矛盾しないか" in block
    assert "処刑がなかった日に霊媒結果を捏造しない" in block
    assert "自分護衛" in block
    assert "同一対象連続護衛" in block
    assert "死亡済み対象への護衛" in block


def test_game_rules_block_states_day1_fake_seer_must_be_white() -> None:
    """NIGHT_0 random-white provenance forces day-1 fake-seer first result
    to be white. The shared rules must say so explicitly so any fake CO seat
    sees it."""
    block = _build_game_rules_block()
    assert "NIGHT_0" in block
    assert "初回" in block
    assert "day 1" in block
    assert "必ず白を主張" in block


def test_game_rules_block_states_day1_black_claim_is_breakdown() -> None:
    """A day-1 first-result-black claim contradicts NIGHT_0 timeline and must
    be framed as breakdown so wolves/madmen don't try it."""
    block = _build_game_rules_block()
    assert "初回結果を黒" in block
    assert "NIGHT_0 タイムラインと矛盾" in block
    assert "破綻" in block
    assert "day 1 で初回黒主張はしない" in block


def test_game_rules_block_defers_fake_black_to_day_2_plus() -> None:
    """Fake black is allowed only from day 2+, with timeline integrity checks."""
    block = _build_game_rules_block()
    assert "偽占い師の黒結果主張は day 2 以降" in block
    assert "前夜に占ったという想定" in block
    assert "対抗 CO の発表内容と矛盾しない" in block


def test_game_rules_block_contains_enthusiast_checklist() -> None:
    """Shared rules must carry the 発言の根拠チェックリスト so every seat sees
    CO history / divination history / vote history / attack pattern / rope
    count / own information scope as the grounding menu, and must cap speech
    to 1–2 concrete points rather than long internal monologue."""
    block = _build_game_rules_block()
    assert "CO 履歴" in block
    assert "判定履歴" in block
    assert "投票履歴" in block
    assert "噛み筋" in block
    assert "縄数" in block
    assert "情報範囲" in block
    assert "1〜2 点" in block


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
    """Bare `相方` (actor mode, partner-known) and `襲撃先を揃える` are wolf-
    playbook vocabulary and must only appear in the werewolf strategy block —
    never in any other role's tips. The inference noun `相方候補` is allowed
    in non-wolf strategies as public-log inference language."""
    block = _build_strategy_block(role)
    if role is Role.WEREWOLF:
        assert "相方" in block
        assert "襲撃先を揃える" in block
    else:
        assert not re.search(r"相方(?!候補)", block), (
            f"bare '相方' (actor mode) leaked into {role.name}"
        )
        assert "襲撃先を揃える" not in block, (
            f"wolf coordination '襲撃先を揃える' leaked into {role.name}"
        )


def test_madman_strategy_prohibits_not_assumes_wolf_positions() -> None:
    """The madman must NOT be told that wolf positions are known — only that
    the opposite is true. The strategy text phrases this as a prohibition, so
    the full prohibition phrase must be present and wolf-coordination tips
    must still be absent (the madman does not get the wolves' playbook).
    `相方候補` (inference noun) is allowed since the madman reasons from
    public logs about who B might be if A is wolf."""
    block = _build_strategy_block(Role.MADMAN)
    assert "人狼位置を知っている前提で話してはならない" in block
    # No wolf-coordination playbook leaks: bare 相方 (known partner) absent;
    # 相方候補 (public-log inference) allowed.
    assert not re.search(r"相方(?!候補)", block)
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


def test_medium_strategy_includes_three_seer_co_elimination_inference() -> None:
    """霊媒師は 3占いCO 盤面で 2 人非狼確定にできるかを毎日整理する。
    霊媒白を非狼確定として数えてよいのは、自分の霊媒 CO 側が真寄りと
    十分読める段階で、霊媒結果以外の整合も併せて説明できる場合に限る。"""
    block = _build_strategy_block(Role.MEDIUM)
    assert "占い師 CO が 3 人" in block
    assert "自分の霊媒 CO 側が真寄りと十分読める段階" in block
    assert "霊媒結果以外の整合" in block
    assert "残る占い師 CO を確定黒級として処刑提案・投票誘導" in block


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
    """Both wolf and madman are warned that piling on fake COs (6+) confirms
    non-CO seats as white — at that point all wolves+madman have CO'd."""
    block = _build_strategy_block(role)
    assert "6 人以上" in block
    assert "騙りすぎ" in block


def test_madman_fake_strategy_has_no_wolf_coordination_vocabulary() -> None:
    """The madman's new fake-CO guidance must not introduce wolf-coordination
    vocabulary (bare `相方`, `襲撃先を揃える`) — the madman does not know the
    real wolf positions. `相方候補` (public-log inference) is allowed."""
    block = _build_strategy_block(Role.MADMAN)
    assert not re.search(r"相方(?!候補)", block)
    assert "襲撃先を揃える" not in block
    # Existing prohibition phrase still present.
    assert "人狼位置を知っている前提で話してはならない" in block


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
def test_fake_strategy_describes_conditional_seer_fake(role: Role) -> None:
    """Day-1 seer fake is offered as a *conditional* option, not an
    unconditional default — the wording must contain gating words
    (無条件 / 潜伏) that signal the LLM to decide based on board state."""
    block = _build_strategy_block(role)
    assert "無条件" in block
    assert "潜伏" in block
    if role is Role.WEREWOLF:
        assert "相方が危険位置" in block
    else:
        # Madman variant: no wolf-coordination vocab (bare 相方 / 襲撃先を揃える),
        # but references CO creep. 相方候補 (inference) is allowed.
        assert "複数の占い師 CO" in block
        assert not re.search(r"相方(?!候補)", block)
        assert "襲撃先を揃える" not in block


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
def test_fake_strategy_anchors_day1_first_result_to_white(role: Role) -> None:
    """Both wolf and madman fake-seer guidance must anchor the day-1 first
    divination result to white per the NIGHT_0 timeline."""
    block = _build_strategy_block(role)
    assert "NIGHT_0 ランダム白" in block
    assert "必ず白を主張" in block


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
def test_fake_strategy_prohibits_day1_black_claim(role: Role) -> None:
    """Both wolf and madman must be told day-1 first-result black breaks the
    NIGHT_0 timeline and must not be claimed."""
    block = _build_strategy_block(role)
    assert "初日に黒を出す主張" in block
    assert "破綻" in block
    assert "絶対にしない" in block


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
def test_fake_strategy_defers_black_call_to_day_2_plus(role: Role) -> None:
    """Both wolf and madman defer fake black to day 2+ with night-divination
    framing."""
    block = _build_strategy_block(role)
    assert "黒出しは day 2 以降" in block
    assert "前夜に占ったという想定" in block


def test_wolf_day1_white_target_integrates_partner_and_attack_pattern() -> None:
    """Wolf-only: day-1 white-target selection must integrate partner position,
    framing risk, and attack-pattern coordination."""
    block = _build_strategy_block(Role.WEREWOLF)
    assert "白先選び" in block
    assert "相方の位置" in block
    assert "囲いリスク" in block
    assert "噛み筋" in block
    assert "襲撃計画" in block


def test_madman_day_2_plus_black_still_carries_misfire_awareness() -> None:
    """Madman-only: even when deferred to day 2+, the black-out misfire risk
    persists because the madman never knows real wolf positions."""
    block = _build_strategy_block(Role.MADMAN)
    assert "誤爆リスクは day 2 以降の黒出しでも常に残る" in block
    # Existing misfire guidance must still be present (no regression).
    assert "誤爆リスク" in block
    assert "白先が本物の狼とは限らない" in block


def test_seer_strategy_covers_proactive_and_counter_co() -> None:
    """Seer must have explicit guidance on early CO when no seer CO has
    appeared, counter-CO against a fake seer with time-ordered history
    disclosure, and the black-pull CO procedure."""
    block = _build_strategy_block(Role.SEER)
    assert "まだ占い師 CO が出ていない" in block
    assert "対抗 CO" in block
    assert "時系列で公開" in block
    assert "黒を引いた場合" in block
    assert "単独真として扱わせてしまう" in block


def test_seer_strategy_includes_three_seer_co_elimination_for_targeting() -> None:
    """占い師は 3占いCO・自分以外の 2 人が非狼確定の盤面で、夜の占い対象選びで
    残る占い師 CO 位置の確認や相方候補ペア仮説を崩す方向を優先する。
    非狼確定として数える根拠は説明可能なものに限る。"""
    block = _build_strategy_block(Role.SEER)
    assert "占い師 CO が 3 人" in block
    assert "自分以外の 2 人が公開情報上で非狼確定" in block
    assert "相方候補ペア仮説を崩す方向" in block
    assert "霊媒結果・襲撃死・CO 破綻など説明可能な根拠に限る" in block


def test_medium_strategy_covers_post_execution_publication_and_counter_co() -> None:
    """Medium must publish results the day after an execution and must run
    counter-CO against a fake medium with time-ordered history framing while
    acknowledging self-roller vulnerability."""
    block = _build_strategy_block(Role.MEDIUM)
    assert "処刑が発生した翌日" in block
    assert "対抗霊媒" in block
    assert "ローラー" in block
    assert "巻き込まれる可能性" in block


def test_knight_strategy_covers_endgame_and_legal_guard_history() -> None:
    """Knight must cover endgame / about-to-be-hung CO timing AND must
    explicitly constrain the guard-diary to the bot's legal guard rules
    (no self-guard, no consecutive guard, no dead-seat guard)."""
    block = _build_strategy_block(Role.KNIGHT)
    assert "終盤" in block
    assert "吊られそう" in block
    assert "護衛履歴を日付順" in block
    assert "自分護衛" in block
    assert "同じ相手の連続護衛" in block
    assert "死亡済み" in block


def test_knight_strategy_covers_multi_axis_guard_evaluation() -> None:
    """Knight strategy must teach a 5-axis comparison rather than fixing a
    single guard target. The five axis tokens must all appear so the LLM can
    weigh candidates rather than always defaulting to the truth-leaning info
    role."""
    block = _build_strategy_block(Role.KNIGHT)
    assert "護衛価値" in block
    assert "襲撃されやすさ" in block
    assert "GJ" in block
    assert "次夜" in block
    assert "連続護衛不可" in block
    assert "説明可能性" in block


def test_knight_strategy_distinguishes_tetsuban_vs_sutekogo() -> None:
    """Knight strategy must define both 鉄板護衛 and 捨て護衛 with their
    bot-specific framing: 鉄板護衛 prioritized when key roles are at risk,
    捨て護衛 always a legal-candidate choice (not skip)."""
    block = _build_strategy_block(Role.KNIGHT)
    assert "鉄板護衛" in block
    assert "捨て護衛" in block
    # 鉄板護衛 bullet must frame it as the priority when key roles are at risk.
    assert "重要役職" in block
    assert "優先" in block
    # 捨て護衛 bullet must explicitly say it is a legal-candidate, 1-name
    # choice — never skip / unsubmitted / no-target.
    assert "合法候補" in block
    assert "1 名" in block
    assert "skip" in block


def test_knight_strategy_warns_against_sutekogo_overuse() -> None:
    """The knight must not adopt 捨て護衛 as a default. The strategy must
    contain a gating sentence that prefers 鉄板護衛 when key roles are at
    immediate risk and explicitly forbids 捨て護衛 as a default action."""
    block = _build_strategy_block(Role.KNIGHT)
    assert "毎夜の既定行動にしない" in block


def test_knight_strategy_includes_three_seer_co_elimination_for_guard_choice() -> None:
    """騎士は 3占いCO・2 人非狼確定が成立したら、残る 1 人からの黒判定で
    守るべき真情報役・確白寄りと、次夜の本命護衛余地を比較する。
    前提が崩れたら護衛方針も再整理する。"""
    block = _build_strategy_block(Role.KNIGHT)
    assert "占い師 CO が 3 人" in block
    assert "次夜の本命護衛余地" in block
    assert "前提が崩れたら護衛方針も再整理" in block


def test_villager_strategy_anchors_in_checklist() -> None:
    """Villager must still forbid CO fakes AND must anchor speech in the
    shared enthusiast checklist (CO / divination / vote histories)."""
    block = _build_strategy_block(Role.VILLAGER)
    assert "CO 騙りは村陣営としては行わない" in block
    assert "CO 履歴" in block
    assert "判定履歴" in block
    assert "1〜2 点" in block


def test_villager_strategy_prohibits_villager_co() -> None:
    """The villager must be explicitly forbidden from declaring '村人CO' /
    '素村CO' / '普通の村人です' / '役職は村人です' as a trust-buy. The block
    must also offer the alternative stance: stay non-CO, reason from public
    information."""
    block = _build_strategy_block(Role.VILLAGER)
    # Forbidden phrases the villager must not say.
    assert "村人CO" in block
    assert "素村CO" in block
    assert "普通の村人です" in block
    assert "役職は村人です" in block
    # Reason: villagers have no ability result so CO carries no proof.
    assert "村人は能力結果を持たない" in block
    # Alternative stance the villager should take instead.
    assert "非 CO の灰" in block
    assert "役職 CO はない" in block


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN, Role.SEER, Role.MEDIUM, Role.KNIGHT])
def test_villager_co_prohibition_does_not_leak_to_other_roles(role: Role) -> None:
    """The villager-CO prohibition is scoped to the villager strategy. Other
    roles must not see '村人CO' / '素村CO' wording — the wolf and madman fake
    seer/medium/knight, never villager; the real seer/medium/knight have their
    own CO playbooks. Cross-leak would either confuse fake-CO planning or
    suppress legitimate role-CO."""
    block = _build_strategy_block(role)
    assert "村人CO" not in block
    assert "素村CO" not in block


def test_villager_strategy_includes_three_seer_co_elimination_inference() -> None:
    """村人は 3占いCO・2非狼確定の盤面で、残る占い師 CO 位置を投票・発言・
    進行提案で固定配役上の狼位置として扱う方針を持つ。非狼確定の数え方の
    厳格さと前提崩壊時の再整理も明示。"""
    block = _build_strategy_block(Role.VILLAGER)
    assert "占い師 CO が 3 人" in block
    assert "残る占い師 CO 位置を投票・発言・進行提案" in block
    assert "印象白や信用未確定 CO の白判定では数えず" in block
    assert "前提が崩れた瞬間" in block


@pytest.mark.parametrize("role", list(Role))
def test_three_seer_co_elimination_role_framings_do_not_cross_leak(role: Role) -> None:
    """役職別 framing 文言が他 role の strategy block へ流れ込まないこと。
    共通ルール本体は `_build_game_rules_block` 側にあるため、strategy 側の
    役職別フレーズが横滑りしてはいけない。"""
    block = _build_strategy_block(role)
    villager_phrase = "残る占い師 CO 位置を投票・発言・進行提案"
    seer_phrase = "相方候補ペア仮説を崩す方向"
    medium_phrase = "残る占い師 CO を確定黒級として処刑提案・投票誘導"
    knight_phrase = "次夜の本命護衛余地"
    if role is not Role.VILLAGER:
        assert villager_phrase not in block, (
            f"villager-framing of 3-seer-CO elimination leaked into {role.name}"
        )
    if role is not Role.SEER:
        assert seer_phrase not in block, (
            f"seer-framing of 3-seer-CO elimination leaked into {role.name}"
        )
    if role is not Role.MEDIUM:
        assert medium_phrase not in block, (
            f"medium-framing of 3-seer-CO elimination leaked into {role.name}"
        )
    if role is not Role.KNIGHT:
        assert knight_phrase not in block, (
            f"knight-framing of 3-seer-CO elimination leaked into {role.name}"
        )


# ----------- 3 役職横断 CO 数・対抗 CO 超過分 推理: role 別運用面の追加
# 共通ルール側の数学的整理 (CO 数 - 1 を超過分として 3 役職分集計、合計 3 で
# 非 CO 位置が確白級) は `_build_game_rules_block` で全 seat に届く。
# 各 role strategy には「その役職ならどう使うか」だけを短く追加する。
def test_villager_strategy_uses_co_overflow_inference() -> None:
    """村人は公開ログから占い師・霊媒師・騎士の CO 数と対抗 CO 超過分を
    毎日整理し、超過分合計 3 なら能力役職 CO していない位置を村陣営の
    確白級として扱い、投票先を CO 群に絞る。"""
    block = _build_strategy_block(Role.VILLAGER)
    assert "対抗 CO 超過分を毎日整理する" in block
    assert "超過分合計が 3 に達したら能力役職 CO していない位置を村陣営の確白級" in block
    assert "投票先を CO 群に絞る" in block
    # 0〜2 と 4 以上の境界条件も村人 framing に含まれていること。
    assert "超過分合計が 0〜2 のうちは" in block
    assert "超過分合計が 4 以上に見えたら" in block


def test_seer_strategy_avoids_wasting_divination_on_non_co_white() -> None:
    """占い師は超過分合計 3 で非 CO 位置が確白級になった場合、そこを
    無駄占いせず、対抗 CO 群やまだ確定しない位置を優先して占う。"""
    block = _build_strategy_block(Role.SEER)
    assert "対抗 CO 超過分合計が 3 に達して能力役職 CO していない位置が非 CO 確白級" in block
    assert "無駄占い" in block
    assert "対抗 CO 群やまだ確定しない位置を優先して占う" in block


def test_medium_strategy_updates_co_inference_via_medium_result() -> None:
    """霊媒師は霊媒結果で CO 数推理を更新する。処刑された CO 者が黒なら
    対抗 CO 群内の狼数を絞り、白なら真役職または狂人の可能性を分け、
    非 CO 確白の前提が保たれるかを確認する (霊媒白=非狼のみのルール維持)。"""
    block = _build_strategy_block(Role.MEDIUM)
    assert "霊媒結果は対抗 CO 超過分の CO 数推理を更新する材料" in block
    assert "対抗 CO 群内の狼数を絞り" in block
    # 霊媒白は非狼だけを示す既存ルールとの整合: 真役職 / 狂人 を分ける。
    assert "白なら真役職または狂人の可能性を分け" in block
    assert "非 CO 確白の前提が保たれるか" in block


def test_knight_strategy_protects_non_co_certified_white() -> None:
    """騎士は超過分合計 3 で生まれた非 CO 確白級や、単独で対抗のない真寄り
    情報役を護衛価値が高い対象として扱う。連続護衛不可・襲撃読み・
    CO 時の説明可能性も合わせて判断。"""
    block = _build_strategy_block(Role.KNIGHT)
    assert "対抗 CO 超過分合計 3 で生まれた非 CO 確白級" in block
    assert "単独で対抗のない真寄り情報役は護衛価値が高い" in block
    # 既存制約 (連続護衛不可・襲撃読み・CO 時の説明可能性) との整合。
    assert "連続護衛不可" in block
    assert "襲撃読み" in block
    assert "CO 時の説明可能性" in block


def test_werewolf_strategy_acknowledges_overcounter_risk() -> None:
    """人狼は能力役職 CO を増やしすぎると、対抗 CO 超過分合計が 3 に達した
    時点で非 CO 位置が村陣営の確白級として扱われ、処刑候補が CO 群に
    集中するリスクを認識する。騙りに出るか潜伏するかは CO 数と残り縄を
    見て、相方と整合する形で選ぶ (相方語彙は wolf 専用)。"""
    block = _build_strategy_block(Role.WEREWOLF)
    assert "対抗 CO 超過分" in block
    assert "超過分合計が 3 に達した時点で" in block
    assert "村陣営の確白級として扱われ" in block
    assert "処刑候補が CO 群に集中する" in block
    assert "相方と整合する形で選ぶ" in block


def test_madman_strategy_acknowledges_overcounter_risk_without_partner_vocab() -> None:
    """狂人は同じリスクを公開情報視点で認識する。本物の人狼位置を
    知っている前提や bare `相方` (actor mode) を使ってはいけない。
    `相方候補` (公開ログからの推理用語) は引き続き許容。"""
    block = _build_strategy_block(Role.MADMAN)
    assert "対抗 CO 超過分" in block
    assert "超過分合計が 3 に達した時点で" in block
    assert "処刑候補が CO 群に集中するリスクを認識する" in block
    # 公開情報視点であることが明示されていること。
    assert "公開情報の各 CO 数と残り縄から判断する" in block
    # Wolf-coordination 語彙の漏れがないこと (既存 leak guard と同形)。
    assert not re.search(r"相方(?!候補)", block), (
        "bare '相方' (actor mode) leaked into madman CO-overflow addition"
    )
    assert "襲撃先を揃える" not in block
    # 既存 prohibition 文言は残ること。
    assert "人狼位置を知っている前提で話してはならない" in block


@pytest.mark.parametrize("role", list(Role))
def test_co_overflow_role_framings_do_not_cross_leak(role: Role) -> None:
    """各 role の CO 超過分 framing 文言が他 role の strategy block へ
    流れ込まないこと。共通ルールの数学的整理は `_build_game_rules_block`
    側にあるため、strategy 側の役職別フレーズが横滑りしてはいけない。"""
    block = _build_strategy_block(role)
    villager_phrase = "投票先を CO 群に絞る"
    seer_phrase = "非 CO 確白級になった場合、そこを無駄占いせず"
    medium_phrase = "霊媒結果は対抗 CO 超過分の CO 数推理を更新する材料"
    knight_phrase = "対抗 CO 超過分合計 3 で生まれた非 CO 確白級"
    werewolf_phrase = "相方と整合する形で選ぶ"
    madman_phrase = "公開情報の各 CO 数と残り縄から判断する"
    if role is not Role.VILLAGER:
        assert villager_phrase not in block, (
            f"villager-framing of CO-overflow inference leaked into {role.name}"
        )
    if role is not Role.SEER:
        assert seer_phrase not in block, (
            f"seer-framing of CO-overflow inference leaked into {role.name}"
        )
    if role is not Role.MEDIUM:
        assert medium_phrase not in block, (
            f"medium-framing of CO-overflow inference leaked into {role.name}"
        )
    if role is not Role.KNIGHT:
        assert knight_phrase not in block, (
            f"knight-framing of CO-overflow inference leaked into {role.name}"
        )
    if role is not Role.WEREWOLF:
        assert werewolf_phrase not in block, (
            f"werewolf-framing of CO-overflow inference leaked into {role.name}"
        )
    if role is not Role.MADMAN:
        assert madman_phrase not in block, (
            f"madman-framing of CO-overflow inference leaked into {role.name}"
        )


# --------------------------------- wolf night-attack guard-aware vocabulary
# Wolf-only tactical phrases for night-attack reasoning. They must appear in
# the werewolf strategy and never leak into another role's strategy or into
# any non-WOLF_ATTACK night-action task.
_WOLF_ATTACK_ONLY_PHRASES = (
    "騎士候補を噛む",
    "護衛リスクを読んで噛む",
)


def test_werewolf_strategy_includes_attack_evaluation_axes() -> None:
    """The wolf gets the 4-axis comparison (value / guard-likelihood /
    knight-candidacy / partner-fit) plus a GJ-risk hook. These are the new
    anchors the LLM uses to weigh each candidate before locking a target."""
    block = _build_strategy_block(Role.WEREWOLF)
    assert "襲撃価値" in block
    assert "護衛されやすさ" in block
    assert "騎士候補度" in block
    assert "GJ" in block or "護衛リスク" in block


def test_werewolf_strategy_includes_attack_approach_taxonomy() -> None:
    """The wolf must be able to label its kami as one of the five recognized
    approaches so the per-day narrative stays consistent."""
    block = _build_strategy_block(Role.WEREWOLF)
    for phrase in ("情報役噛み", "白位置噛み", "意見噛み", "騎士探し", "SG 残し"):
        assert phrase in block, f"wolf strategy missing approach token {phrase!r}"


def test_werewolf_strategy_preserves_partner_convergence() -> None:
    """Adding guard-reading content must not erase the existing partner-align
    rule. `相方` and the `1 人に揃える` directive must both still be present."""
    block = _build_strategy_block(Role.WEREWOLF)
    assert "相方" in block
    assert "1 人に揃える" in block


def test_werewolf_strategy_disclaims_real_role_inference() -> None:
    """Knight-candidate inference must be framed as public-log推定, not as a
    claim about the actual role table. Guards against future copy-paste that
    would imply the wolf knows the real knight seat."""
    block = _build_strategy_block(Role.WEREWOLF)
    assert "実役職を知っている前提で断言してはならない" in block
    assert "公開情報からの推定" in block


@pytest.mark.parametrize(
    "role", [Role.MADMAN, Role.SEER, Role.MEDIUM, Role.KNIGHT, Role.VILLAGER]
)
def test_wolf_attack_only_vocabulary_never_in_non_wolf_strategy(role: Role) -> None:
    """The new tactical phrases are wolf-private. Any leak (e.g. someone copies
    the wolf bullet into the knight strategy by mistake) must trip this guard."""
    block = _build_strategy_block(role)
    for phrase in _WOLF_ATTACK_ONLY_PHRASES:
        assert phrase not in block, (
            f"wolf-only attack vocab {phrase!r} leaked into {role.name}"
        )


# ---------------------------------------------------- night-action task block
def test_task_night_action_wolf_attack_includes_evaluation_checklist() -> None:
    """The WOLF_ATTACK night task hands the LLM the 4-axis checklist inline so
    even an LLM that ignored the strategy block sees the rubric on the action
    turn."""
    text = task_night_action(SubmissionType.WOLF_ATTACK, ["席1 A", "席2 B"])
    assert "襲撃価値" in text
    assert "護衛されやすさ" in text
    assert "騎士候補度" in text
    assert "翌日の説明しやすさ" in text
    assert "騎士探し" in text
    # Existing partner-align nudge is preserved.
    assert "強い反対理由がなければ" in text


@pytest.mark.parametrize("kind", [SubmissionType.SEER_DIVINE, SubmissionType.KNIGHT_GUARD])
def test_task_night_action_non_wolf_excludes_attack_checklist(
    kind: SubmissionType,
) -> None:
    """The seer-divine / knight-guard tasks must not inherit the wolf attack
    checklist — only WOLF_ATTACK gets it."""
    text = task_night_action(kind, ["席1 A", "席2 B"])
    assert "襲撃価値" not in text
    assert "護衛されやすさ" not in text
    assert "騎士候補度" not in text
    assert "騎士探し" not in text
    assert "翌日の説明しやすさ" not in text


def test_task_night_action_seer_divine_includes_targeting_checklist() -> None:
    """SEER_DIVINE task hands the seer a targeting rubric inline. The new axes
    push the LLM beyond random selection from legal candidates and toward
    high-information targets (counter-CO whites, vote oddities, gray-narrowing
    positions). These tokens must reach the LLM via the task block even if the
    role strategy block is somehow ignored."""
    text = task_night_action(SubmissionType.SEER_DIVINE, ["席1 A", "席2 B"])
    # Positive — seer-targeting axes anchors must be present.
    assert "占い価値" in text
    assert "灰を狭める" in text
    assert "対抗 CO" in text
    assert "囲い候補" in text
    assert "投票" in text
    assert "白でも黒でも情報が落ちる" in text
    # Negative — none of the wolf-task forbidden substrings may appear here.
    # (The parametrized exclusion test below also covers this; this focused
    # negative makes the contract explicit at the SEER_DIVINE call site.)
    assert "襲撃価値" not in text
    assert "護衛されやすさ" not in text
    assert "騎士候補度" not in text
    assert "翌日の説明しやすさ" not in text
    assert "騎士探し" not in text


def test_task_night_action_knight_guard_includes_evaluation_checklist() -> None:
    """KNIGHT_GUARD task hands the knight a 5-axis checklist inline so even an
    LLM that ignored the strategy block sees the rubric on the action turn.
    Critical: the knight task must NOT reuse the wolf-attack vocabulary
    (襲撃価値 / 護衛されやすさ / 騎士候補度 / 翌日の説明しやすさ / 騎士探し) —
    those would trip the existing leak guard. The knight uses its own
    parallel vocabulary (鉄板護衛すべき価値 / 今夜実際に噛まれそうか /
    次夜に同じ相手を守れないリスク / 捨て護衛で本命護衛余地を残す価値)."""
    text = task_night_action(SubmissionType.KNIGHT_GUARD, ["席1 A", "席2 B"])
    # Positive — knight checklist anchors must reach the LLM via the task block.
    assert "鉄板護衛" in text
    assert "捨て護衛" in text
    assert "次夜" in text
    assert "GJ" in text
    # 捨て護衛 must be framed as a legal-candidate, 1-name selection (not skip).
    assert "1 名" in text
    assert "skip" in text
    # Negative — none of the wolf-task forbidden substrings may appear here.
    assert "襲撃価値" not in text
    assert "護衛されやすさ" not in text
    assert "騎士候補度" not in text
    assert "翌日の説明しやすさ" not in text
    assert "騎士探し" not in text


# ----------------------------------------------------- wolf-chat task block
def test_task_wolf_chat_includes_guard_and_knight_candidate_reasons() -> None:
    """Wolf-chat coordination must elicit *why* (guard risk, knight-candidate,
    approach taxonomy, agree/disagree) — not just *who*. The 1人に揃える
    directive and 80–150 char budget remain so partners still converge."""
    text = task_wolf_chat(["席3 P"], ["席1 A", "席2 B"])
    assert "護衛リスク" in text
    assert "騎士候補" in text
    assert "賛否" in text
    assert "1 人に揃える" in text
    assert "80〜150 字" in text
    # All five approach tokens must be offered as labels for the reason.
    for phrase in ("情報役噛み", "白位置噛み", "意見噛み", "騎士探し", "SG 残し"):
        assert phrase in text, f"wolf-chat task missing approach token {phrase!r}"


# ----------------------------------------------------- daytime speech task
def test_task_daytime_speech_default_omits_first_round_rule() -> None:
    """Default call (no `discussion_round`, no `role`) — runoff and any
    backward-compat caller — must NOT include the day-2+ first-round mandatory
    result rule, NOR the day-1 wolf-side fake-CO selection nudge."""
    text = task_daytime_speech(2)
    assert "1 巡目" not in text
    assert "前夜の能力結果" not in text
    assert "占い師騙り・霊媒師騙り・潜伏の 3 択" not in text


def test_task_daytime_speech_day1_round1_omits_day2_rule() -> None:
    """Day 1 round 1 must NOT include the day-2+ rule. Day 1 first results are
    already constrained by NIGHT_0 random white / day-1 first white in the
    game rules block; the day-2+ rule does not apply on day 1. The default
    (no `role`) call must also not include the wolf-side fake-CO selection
    nudge — only WEREWOLF / MADMAN callers see it."""
    text = task_daytime_speech(1, discussion_round=1)
    assert "1 巡目" not in text
    assert "前夜の能力結果" not in text
    assert "占い師騙り・霊媒師騙り・潜伏の 3 択" not in text


def test_task_daytime_speech_day2_round2_omits_first_round_rule() -> None:
    """Round 2 of day 2 must NOT carry the 'must attach prior-night results'
    imperative. Round 2 is for follow-ups; the first-round publication
    requirement is round-1 only."""
    text = task_daytime_speech(2, discussion_round=2)
    assert "1 巡目" not in text
    assert "前夜の能力結果" not in text


def test_task_daytime_speech_day2_round1_publishes_prior_night_results() -> None:
    """Day 2+ round 1: CO'd or about-to-CO seer/medium/knight LLM must attach
    prior-night ability results. The task text must call out all three roles
    and their respective result formats so the LLM can match its own role."""
    text = task_daytime_speech(2, discussion_round=1)
    # Headline tokens.
    assert "day 2 以降" in text
    assert "1 巡目" in text
    assert "前夜" in text
    assert "能力結果" in text
    # All three info roles must be addressed.
    assert "占い師" in text
    assert "霊媒師" in text
    assert "騎士" in text
    # Result format anchors.
    assert "白/黒" in text
    assert "前日処刑者" in text
    assert "結果なし" in text
    assert "合法な護衛履歴" in text
    assert "平和な朝" in text
    # Trust-pressure framing — withholding lowers credibility.
    assert "信用低下" in text or "破綻" in text
    # Wolf-coordination vocabulary must NOT appear in this task block (every
    # role sees this task text). Bare 相方 (actor mode) absent; 相方候補
    # (public-log inference noun) is allowed in the daytime speech task.
    assert not re.search(r"相方(?!候補)", text)
    assert "襲撃先を揃える" not in text


@pytest.mark.parametrize("day", [3, 5])
def test_task_daytime_speech_day_n_round1_publishes_prior_night_results(day: int) -> None:
    """The 'day 2 以降' rule applies on day 3, 5, etc. as well — verifies the
    `day_number >= 2` gate, not a literal day-2 check."""
    text = task_daytime_speech(day, discussion_round=1)
    assert "day 2 以降" in text
    assert "1 巡目" in text
    assert "能力結果" in text


def test_task_daytime_speech_base_contract_preserved() -> None:
    """Every variant must still surface the base intent/skip + 80–300 char
    contract — neither the day-2+ branch nor the default branch may erase it."""
    for kwargs in (
        {"day_number": 1},
        {"day_number": 2},
        {"day_number": 1, "discussion_round": 1},
        {"day_number": 2, "discussion_round": 1},
        {"day_number": 2, "discussion_round": 2},
    ):
        text = task_daytime_speech(**kwargs)
        assert "intent=speak" in text
        assert "intent=skip" in text
        assert "80〜300 字" in text


# ------------------------------------------------ day 2+ round 1 fake publication
@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
def test_fake_strategy_day2_round1_publishes_prior_night_results(role: Role) -> None:
    """Both wolf and madman, when faking seer/medium/knight on day 2+, must be
    instructed to publish the prior-night ability result on the day-2+ round-1
    speech. Headline tokens (1 巡目, 前夜, 能力結果) must appear."""
    block = _build_strategy_block(role)
    assert "day 2 以降" in block
    assert "1 巡目" in block
    assert "前夜" in block
    assert "能力結果" in block


def test_werewolf_fake_strategy_integrates_with_attack_plan() -> None:
    """Wolf-only: the fake-result guidance must integrate with the wolf's
    private knowledge (partner position, framing risk, attack pattern) and
    cross-checks (medium results, vote history, legal guard history)."""
    block = _build_strategy_block(Role.WEREWOLF)
    assert "相方" in block
    assert "囲い" in block
    assert "噛み筋" in block
    assert "霊媒結果" in block
    assert "合法な護衛履歴" in block


def test_madman_fake_strategy_acknowledges_misfire_and_legal_constraints() -> None:
    """Madman-only: the fake-result guidance must include misfire awareness
    (誤爆リスク, 白先が本物の狼とは限らない), the no-execution-no-result rule,
    and the legal-guard-history constraint. Wolf-coordination vocabulary
    (bare `相方`, `襲撃先を揃える`) must remain absent; `相方候補` (public-log
    inference) is allowed in the new pair-inference bullets."""
    block = _build_strategy_block(Role.MADMAN)
    assert "誤爆リスク" in block
    assert "白先が本物の狼とは限らない" in block
    assert "処刑なしの日は結果なし" in block
    assert "合法な護衛履歴" in block
    # Existing leak guard: bare 相方 absent; 相方候補 (inference) allowed.
    assert not re.search(r"相方(?!候補)", block)
    assert "襲撃先を揃える" not in block


# ------------------------------------------ day 1 medium fake-CO / 2-2 / 1-2 layout
@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
def test_fake_strategy_offers_medium_fake_as_day1_option(role: Role) -> None:
    """Wolf and madman must teach day-1 medium fake-CO as a real choice
    alongside seer fake and 潜伏 — not just a day-2+ post-hoc move. The
    existing day-2+ post-hoc framing must remain (no regression)."""
    block = _build_strategy_block(role)
    assert "霊媒師騙りも day 1 の選択肢" in block
    assert "占い師騙り" in block
    assert "潜伏" in block
    assert "対抗占い師 CO が出ている場合は、day 2 以降に霊媒師騙り" in block


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
def test_fake_strategy_frames_2_2_and_1_2_as_engineerable_layouts(role: Role) -> None:
    """Wolf-side strategy must frame 2-2 and 1-2 as boards the wolf side can
    engineer through medium fake-CO, not just descriptive labels. The
    creation-mode form `2-2 を作` is the directive anchor."""
    block = _build_strategy_block(role)
    assert "1-2" in block
    assert "2-2 を作" in block
    assert "霊媒ローラー" in block


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
def test_fake_strategy_day1_medium_co_does_not_publish_result(role: Role) -> None:
    """day-1 medium fake-CO must explicitly NOT publish an execution result —
    no execution has happened yet. This composes with (does not replace) the
    existing day-2+ '前日処刑者だけに結果を出す' rule."""
    block = _build_strategy_block(role)
    assert "day 1 に霊媒師騙りで CO する場合" in block
    assert "まだ処刑が発生していない" in block
    assert "存在しない初日結果を捏造しない" in block


def test_madman_medium_fake_carries_misfire_awareness_for_both_colors() -> None:
    """Madman-only: the medium-fake guidance must say even when picking
    medium-black or medium-white, the madman never knows real wolf positions,
    so 誤爆 / 誤支援 risk persists for both colors. Wolf-coordination
    vocabulary stays absent."""
    block = _build_strategy_block(Role.MADMAN)
    assert "霊媒黒で本物の狼を切ってしまう" in block
    assert "霊媒白で真占いを補強してしまう" in block
    assert "誤爆" in block
    assert "誤支援" in block
    # Leak guard preserved.
    assert not re.search(r"相方(?!候補)", block)
    assert "襲撃先を揃える" not in block


@pytest.mark.parametrize("role", list(Role))
def test_layout_creation_directives_only_in_wolf_madman_strategy(role: Role) -> None:
    """The wolf-side directive form '2-2 を作' / '1-2 を作' / '霊媒師騙りを選'
    is creation-mode wolf-side guidance and must appear only in WEREWOLF and
    MADMAN strategies — never in SEER / MEDIUM / KNIGHT / VILLAGER. The
    descriptive board labels in the shared rules block are not wolf-side
    directives and are unaffected by this guard."""
    block = _build_strategy_block(role)
    if role in (Role.WEREWOLF, Role.MADMAN):
        # Creation directive must reach wolf side.
        assert "2-2 を作" in block
    else:
        assert "2-2 を作" not in block, f"'2-2 を作' (creation directive) leaked into {role.name}"
        assert "1-2 を作" not in block, f"'1-2 を作' (creation directive) leaked into {role.name}"
        assert "霊媒師騙りを選" not in block, (
            f"'霊媒師騙りを選' (wolf-side selection directive) leaked into {role.name}"
        )


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
def test_task_daytime_speech_day1_round1_wolf_or_madman_offers_medium_fake_as_option(
    role: Role,
) -> None:
    """At day-1 round-1 only, the daytime speech task injects a 3-way fake-CO
    selection nudge for wolf and madman — a per-call task-level reminder that
    re-surfaces the day-1 selection at the moment of speech generation.
    Default (no role) and other rounds must not include it (verified by
    sibling default-omission tests)."""
    text = task_daytime_speech(1, discussion_round=1, role=role)
    assert "占い師騙り・霊媒師騙り・潜伏の 3 択" in text
    assert "1-2" in text
    assert "2-2 を作" in text
    # Leak-guard regression — no wolf-coordination vocab in the task block.
    assert not re.search(r"相方(?!候補)", text)
    assert "襲撃先を揃える" not in text


# ------------------------------------------------ seer night-divination axes
def test_seer_strategy_includes_night_divination_targeting_axes() -> None:
    """The true seer strategy must teach the LLM to pick high-information
    targets rather than randomizing over legal candidates. The axes echo the
    SEER_DIVINE task block so the LLM sees them in both places."""
    block = _build_strategy_block(Role.SEER)
    assert "占い価値" in block
    assert "灰を狭める" in block
    assert "対抗 CO" in block
    assert "囲い候補" in block
    assert "投票" in block
    assert "白でも黒でも情報が落ちる" in block
    # Existing SEER unique anchor (cross-leak pivot) must still be present.
    assert "判定履歴を時系列で一貫" in block


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


# =========================================================================
# build_user_context — rope block + raw-log passthrough invariants
# =========================================================================


def _ctx_seat(seat_no: int, name: str) -> Seat:
    return Seat(
        seat_no=seat_no,
        display_name=name,
        discord_user_id=f"u{seat_no}",
        is_llm=False,
        persona_key=None,
    )


def _ctx_player(seat_no: int, *, role: Role | None = None, alive: bool = True) -> Player:
    return Player(seat_no=seat_no, role=role, alive=alive)


def _ctx_game(*, phase: Phase = Phase.DAY_DISCUSSION, day: int = 1) -> Game:
    return Game(
        id="g-ctx",
        guild_id="gu",
        host_user_id="h",
        phase=phase,
        day_number=day,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )


def _ctx_speech(text: str, *, actor_seat: int, day: int = 1) -> dict[str, object]:
    return {"kind": "PLAYER_SPEECH", "text": text, "actor_seat": actor_seat, "day": day}


def test_build_user_context_no_co_parser_sections() -> None:
    """The pre-digest CO parser sections (CO list / 盤面分類 / 役職推定メモ) must
    not appear in user_context. Even seeded with declarative-looking PLAYER_SPEECH
    text, the build no longer attaches machine-summarized CO output — the LLM
    is expected to read the raw 公開ログ要約 and judge in context."""
    seats = [_ctx_seat(1, "Alice"), _ctx_seat(2, "Bob"), _ctx_seat(3, "Carol")]
    players = [_ctx_player(1), _ctx_player(2), _ctx_player(3)]
    out = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[
            _ctx_speech("占い師CO", actor_seat=1),
            _ctx_speech("霊媒師CO", actor_seat=2),
        ],
        private_logs=[],
    )
    assert "## CO・判定の機械整理" not in out
    assert "## 盤面分類" not in out
    assert "## 役職推定メモ (公開情報ベース)" not in out
    # The parser-style "占い師CO: 席X" / "霊媒師CO: 席X" / "公開CO履歴ベース"
    # rendering lines are the canonical anti-pattern; not produced anywhere now.
    assert "占い師CO: 席" not in out
    assert "霊媒師CO: 席" not in out
    assert "公開CO履歴ベース" not in out


def test_build_user_context_topical_co_phrase_does_not_create_self_co() -> None:
    """A speaker who merely raises `占いCO` as a topic / hypothetical must not
    be tagged as a self-CO. The raw text still passes through 公開ログ要約 so
    the LLM can read it in context."""
    seats = [_ctx_seat(1, "Alice"), _ctx_seat(2, "Bob")]
    players = [_ctx_player(1), _ctx_player(2)]
    speech = "占いCOが出たらどう見るか考えたい"
    out = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[_ctx_speech(speech, actor_seat=2)],
        private_logs=[],
    )
    assert "占い師CO: 席2" not in out
    # Raw speech survives in the public log dump.
    assert speech in out


def test_build_user_context_topical_medium_phrase_does_not_create_self_co() -> None:
    """Same invariant for medium-CO topical mentions — `霊媒COについて…` is a
    discussion prompt, not a self-CO."""
    seats = [_ctx_seat(1, "Alice"), _ctx_seat(2, "Bob"), _ctx_seat(3, "Carol")]
    players = [_ctx_player(1), _ctx_player(2), _ctx_player(3)]
    speech = "霊媒COについてどう見る？"
    out = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[_ctx_speech(speech, actor_seat=3)],
        private_logs=[],
    )
    assert "霊媒師CO: 席3" not in out
    assert speech in out


def test_build_user_context_passes_raw_player_speech_through() -> None:
    """The raw PLAYER_SPEECH text reaches user_context with the seat-attributed
    `[PLAYER_SPEECH] 席N {name}: …` formatting, so the LLM can spot CO
    declarations on its own."""
    seats = [_ctx_seat(1, "Alice"), _ctx_seat(2, "Bob")]
    players = [_ctx_player(1), _ctx_player(2)]
    out = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[_ctx_speech("占い師COします。 席2 Bob 白", actor_seat=1)],
        private_logs=[],
    )
    assert "[PLAYER_SPEECH] 席1 Alice: 占い師COします。 席2 Bob 白" in out


def test_build_user_context_rope_count_with_alive_dead_breakdown() -> None:
    seats = [_ctx_seat(i, f"S{i}") for i in range(1, 10)]
    # 7 alive, 2 dead → 3 ropes.
    players = [_ctx_player(i, alive=True) for i in range(1, 8)] + [
        _ctx_player(i, alive=False) for i in range(8, 10)
    ]
    out = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[],
        private_logs=[],
    )
    assert "## 縄数・PP/RPPリスク" in out
    assert "生存 7 人 / 死亡 2 人" in out
    assert "3 縄" in out


@pytest.mark.parametrize(
    "alive, dead, ropes",
    [(9, 0, 4), (7, 2, 3), (5, 4, 2), (3, 6, 1), (2, 7, 0)],
)
def test_build_user_context_rope_summary_parametrized(alive: int, dead: int, ropes: int) -> None:
    """Rope formula coverage migrated from the deleted test_llm_context_analysis.py.
    Asserts against the user_context block directly so the helper is exercised
    through its only public surface."""
    seats = [_ctx_seat(i, f"S{i}") for i in range(1, alive + dead + 1)]
    players = [_ctx_player(i, alive=True) for i in range(1, alive + 1)] + [
        _ctx_player(i, alive=False) for i in range(alive + 1, alive + dead + 1)
    ]
    out = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[],
        private_logs=[],
    )
    assert "## 縄数・PP/RPPリスク" in out
    assert f"生存 {alive} 人 / 死亡 {dead} 人" in out
    assert f"{ropes} 縄" in out
    assert "9人村開始時は4縄" in out


def test_build_user_context_rope_endgame_note_at_3_alive() -> None:
    seats = [_ctx_seat(i, f"S{i}") for i in range(1, 10)]
    players = [_ctx_player(i, alive=True) for i in range(1, 4)] + [
        _ctx_player(i, alive=False) for i in range(4, 10)
    ]
    out = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[],
        private_logs=[],
    )
    assert "最終局面" in out


def test_build_user_context_rope_pp_warning_at_5_alive() -> None:
    seats = [_ctx_seat(i, f"S{i}") for i in range(1, 10)]
    players = [_ctx_player(i, alive=True) for i in range(1, 6)] + [
        _ctx_player(i, alive=False) for i in range(6, 10)
    ]
    out = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[],
        private_logs=[],
    )
    assert "PP/RPP" in out


def test_build_user_context_rope_normal_note_at_9_alive() -> None:
    seats = [_ctx_seat(i, f"S{i}") for i in range(1, 10)]
    players = [_ctx_player(i, alive=True) for i in range(1, 10)]
    out = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[],
        private_logs=[],
    )
    assert "通常進行" in out


def test_build_user_context_block_order_after_co_parser_removal() -> None:
    """After dropping the CO parser, the surviving block order is:
    rope block → 私的メモ → 公開ログ要約 → 自分の直近の発言. The deleted
    parser headings must be absent."""
    seats = [_ctx_seat(1, "Alice")]
    players = [_ctx_player(1)]
    out = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[],
        private_logs=[],
    )
    rope_idx = out.find("## 縄数・PP/RPPリスク")
    priv_idx = out.find("## あなたの私的メモ")
    pub_idx = out.find("## 公開ログ要約")
    own_idx = out.find("## 自分の直近の発言")
    assert -1 < rope_idx < priv_idx < pub_idx < own_idx
    assert "## CO・判定の機械整理" not in out
    assert "## 盤面分類" not in out
    assert "## 役職推定メモ (公開情報ベース)" not in out


def test_build_user_context_wolf_partner_block_only_for_wolves() -> None:
    """Regression: the wolf-partner block remains gated to werewolves after the
    CO-parser analysis section was removed from between it and the rest."""
    seats = [_ctx_seat(1, "Alice"), _ctx_seat(2, "Bob")]
    players = [
        _ctx_player(1, role=Role.WEREWOLF),
        _ctx_player(2, role=Role.WEREWOLF),
    ]
    villager_seat = _ctx_seat(3, "Carol")
    villager = _ctx_player(3, role=Role.VILLAGER)
    seats3 = [*seats, villager_seat]
    players3 = [*players, villager]

    wolf_view = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats3,
        players=players3,
        public_logs=[],
        private_logs=[],
    )
    villager_view = build_user_context(
        game=_ctx_game(),
        me=villager,
        my_seat=villager_seat,
        seats=seats3,
        players=players3,
        public_logs=[],
        private_logs=[],
    )
    assert "## 仲間の人狼" in wolf_view
    assert "## 仲間の人狼" not in villager_view


# ----------------------------- werewolf vote-discipline strategy & task_vote


def test_wolf_strategy_block_contains_vote_discipline_vocabulary() -> None:
    """The werewolf strategy block must teach 投票 discipline: 票筋 reads,
    身内票/ライン切り on the partner when unsavable, 票逸らし risk, and the
    決選投票 PP/RPP comparison. Without these, the LLM wolf falls back to
    transparent partner-protection that exposes the wolf line."""
    block = _build_strategy_block(Role.WEREWOLF)
    for token in (
        "身内票",
        "ライン切り",
        "票筋",
        "処刑濃厚",
        "票を逸ら",
        "決選投票",
        "PP/RPP",
        "透け",
    ):
        assert token in block, f"wolf strategy missing vote-discipline token {token!r}"
    assert "相方を救" in block
    assert "相方を切" in block


@pytest.mark.parametrize(
    "role",
    [Role.MADMAN, Role.SEER, Role.MEDIUM, Role.KNIGHT, Role.VILLAGER],
)
def test_non_wolf_strategy_excludes_partner_action_vocabulary(role: Role) -> None:
    """Partner-name and partner-action vocabulary must stay wolf-only. Bare
    `身内票` / `ライン切り` are shared in `_build_game_rules_block`, but the
    wolf-specific phrasings (`仲間の人狼`, `相方を救う`, `相方を切る`) must
    not appear in any other role's strategy block."""
    block = _build_strategy_block(role)
    assert "仲間の人狼" not in block, f"'仲間の人狼' leaked into {role.name}"
    assert "相方を救" not in block, f"'相方を救' leaked into {role.name}"
    assert "相方を切" not in block, f"'相方を切' leaked into {role.name}"


def test_task_vote_baseline_unchanged_without_role() -> None:
    """The default `task_vote(candidates, runoff)` call must keep the existing
    base text and never inject partner-action vocabulary, so all pre-existing
    call sites and non-wolf voters get the same instructions as before. Bare
    `相方` (actor mode, partner-known) and `仲間の人狼` must not appear; the
    inference noun `相方候補` is allowed in the shared pair-inference bullet
    of the common path."""
    text = task_vote(["席1 A", "席2 B"], runoff=False)
    assert "投票先として合法な候補は: 席1 A、席2 B" in text
    assert "仲間の人狼" not in text
    assert not re.search(r"相方(?!候補)", text)


def test_task_vote_wolf_path_emits_partner_checklist() -> None:
    """A wolf voter with at least one alive partner gets the partner-aware
    vote checklist appended after the base candidate text."""
    text = task_vote(
        ["席1 A", "席2 B"],
        runoff=False,
        role=Role.WEREWOLF,
        wolf_partner_tokens=["席2 B"],
    )
    for token in (
        "仲間の人狼",
        "席2 B",
        "相方",
        "身内票",
        "ライン切り",
        "票筋",
        "処刑濃厚",
        "透け",
        "合法候補",
    ):
        assert token in text, f"wolf vote task missing token {token!r}"


def test_task_vote_wolf_runoff_adds_runoff_checklist() -> None:
    """The wolf runoff vote task must add the 決選投票-specific tradeoff
    comparison (救済成功見込み・透けリスク・PP/RPP) on top of the base
    wolf checklist."""
    text = task_vote(
        ["席1 A", "席2 B"],
        runoff=True,
        role=Role.WEREWOLF,
        wolf_partner_tokens=["席2 B"],
    )
    for token in ("決選投票", "透け", "PP/RPP"):
        assert token in text, f"wolf runoff vote task missing token {token!r}"


@pytest.mark.parametrize(
    "role",
    [Role.VILLAGER, Role.MADMAN, Role.SEER, Role.MEDIUM, Role.KNIGHT],
)
def test_task_vote_non_wolf_role_returns_base_text(role: Role) -> None:
    """Passing a non-wolf role to `task_vote` returns the base text — the
    wolf-specific block never reaches non-wolf voters even if a future caller
    forgets to skip the role argument. Bare `相方` (actor mode) and `仲間の人狼`
    must be absent; the inference noun `相方候補` is allowed in the common path."""
    text = task_vote(["席1 A", "席2 B"], runoff=False, role=role)
    assert "仲間の人狼" not in text
    assert not re.search(r"相方(?!候補)", text)


def test_task_vote_madman_with_partner_token_does_not_leak_partner() -> None:
    """Even if a caller naively passes a partner token for a madman, the
    role gate inside `task_vote` must drop it. The madman never sees real
    wolf positions."""
    text = task_vote(
        ["席1 A"],
        runoff=False,
        role=Role.MADMAN,
        wolf_partner_tokens=["席3 X"],
    )
    assert "仲間の人狼" not in text
    assert "席3 X" not in text


def test_task_vote_lone_wolf_returns_base_text() -> None:
    """A wolf voter with no alive partner (lone-wolf endgame) gets the base
    text — the partner-aware checklist requires at least one partner token.
    Bare `相方` (actor mode) and `仲間の人狼` must be absent; `相方候補`
    (inference) is allowed in the shared pair-inference bullet."""
    text = task_vote(
        ["席1 A"],
        runoff=False,
        role=Role.WEREWOLF,
        wolf_partner_tokens=(),
    )
    assert "仲間の人狼" not in text
    assert not re.search(r"相方(?!候補)", text)


# ----------------------------------- 2 人狼ペア推理 (wolf-pair inference)
# Pair-inference asks every LLM seat to think paired: "if A is wolf, who is
# the natural B?" The vocabulary `相方候補` (inference noun) is shared across
# all roles via the rules block; `相方` (actor mode, partner-known) stays
# strictly in the werewolf strategy and the wolf-only vote/night blocks.

_PAIR_INFERENCE_RULES_TOKENS = (
    "2 人狼",
    "相方候補",
    "公開ログからの仮説",
    "単体黒要素",
    "票筋",
    "噛み筋",
    "ライン切り",
    "身内票",
)


def test_game_rules_block_introduces_two_wolf_pair_inference() -> None:
    """The shared rules block must teach paired-wolf inference: build an A-B
    hypothesis from public logs (vote tracks, attack patterns, line-cuts,
    inner-circle votes) and weigh single-wolf suspicion against partner fit."""
    block = _build_game_rules_block()
    for token in _PAIR_INFERENCE_RULES_TOKENS:
        assert token in block, f"rules block missing pair-inference token {token!r}"
    # Soft-cap on speech length must stay; the new bullets must not encourage
    # exhaustive pair-listing speeches.
    assert "1〜2 点" in block


def test_villager_strategy_includes_pair_inference_vocabulary() -> None:
    """The villager (public-information-only seat) must reason paired:
    candidate + 相方候補 from public logs, plus observation patterns
    (身内票, 票逸らし, ライン切り) as wolf-detection signals. Bare `相方`
    must not appear; the villager-CO ban must remain intact."""
    block = _build_strategy_block(Role.VILLAGER)
    assert "相方候補" in block
    assert "2 人狼仮説" in block
    assert "身内票" in block
    # Either of the observation-pattern phrases is sufficient.
    assert "票逸ら" in block or "ライン切り" in block
    # Bare `相方` (actor mode) must not appear.
    assert not re.search(r"相方(?!候補)", block)
    # Existing villager-CO ban remains.
    assert "村人 CO 禁止" in block or "村人CO" in block


def test_seer_strategy_includes_pair_inference_for_target_selection() -> None:
    """Seer divine-target selection must factor in pair-inference: white-or-
    black both organize 相方候補 hypotheses, counter-CO comparison drives the
    strongest pair to break. Bare `相方` (actor mode) must not appear."""
    block = _build_strategy_block(Role.SEER)
    assert "相方候補" in block
    assert "2 人狼仮説" in block
    assert "対抗" in block
    assert not re.search(r"相方(?!候補)", block)


def test_medium_strategy_includes_pair_inference_after_execution() -> None:
    """Medium pair-inference: medium-black narrows the executed wolf's 相方候補
    via 票筋 / 庇い / ライン切り / 身内票; medium-white examines who pushed the
    execution. Bare `相方` (actor mode) must not appear."""
    block = _build_strategy_block(Role.MEDIUM)
    assert "相方候補" in block
    assert "票筋" in block
    assert "身内票" in block
    assert "2 人狼仮説" in block
    assert not re.search(r"相方(?!候補)", block)


def test_knight_strategy_includes_pair_inference_for_guard_selection() -> None:
    """Knight pair-inference: guard-target selection considers which 2-wolf
    hypothesis benefits from the kami; peaceful mornings + actual attack
    targets feed back into the daytime pair-inference. Bare `相方` (actor
    mode) must not appear."""
    block = _build_strategy_block(Role.KNIGHT)
    assert "相方候補" in block
    assert "2 人狼仮説" in block
    assert "噛み筋" in block
    assert not re.search(r"相方(?!候補)", block)


def test_werewolf_strategy_includes_two_wolf_set_framing() -> None:
    """The werewolf strategy must explicitly tell the wolf that it and its
    partner are read as a 2-wolf set by the village — every save/cut/cover/
    black-out/attack must be defensible as the same A-B line on later days.
    `視点漏れ` is the absolute prohibition (don't leak partner-known info)."""
    block = _build_strategy_block(Role.WEREWOLF)
    assert "2 人狼セット" in block
    assert "視点漏れ" in block
    assert "ライン" in block
    # Bare `相方` is REQUIRED in the wolf strategy (actor mode, partner-known).
    assert "相方" in block


def test_madman_strategy_includes_pair_inference_with_misfire() -> None:
    """The madman, lacking real-wolf-position knowledge, must reason 2-wolf
    hypotheses from public logs only — with explicit misfire framing. Bare
    `相方` (actor mode) and `襲撃先を揃える` (wolf-only night coordination)
    must remain absent."""
    block = _build_strategy_block(Role.MADMAN)
    assert "2 人狼仮説" in block
    assert "公開ログからの推定" in block
    assert "誤爆" in block
    assert "相方候補" in block
    # Wolf-coordination vocab forbidden.
    assert not re.search(r"相方(?!候補)", block)
    assert "襲撃先を揃える" not in block
    # Existing prohibition phrase still present.
    assert "人狼位置を知っている前提で話してはならない" in block


def test_task_daytime_speech_default_includes_pair_inference_softly() -> None:
    """The daytime speech task's base body teaches paired suspicion as a
    soft hint — not every speech must list pairs. The softness modifiers
    (`必要に応じて` / `1〜2 点`) guard against speech inflation. The base
    contract (80–300 chars, intent=speak/skip) must be preserved."""
    text = task_daytime_speech(2)
    assert "相方候補" in text
    assert "2 人狼セット" in text
    # Softness anchors so every speech doesn't balloon into pair listing.
    assert "必要に応じて" in text
    assert "1〜2 点" in text
    # Existing base contract preserved.
    assert "80〜300 字" in text
    assert "intent=speak" in text
    assert "intent=skip" in text


def test_task_vote_base_includes_pair_inference_for_all_roles() -> None:
    """The common-path `task_vote` body (no role argument) must teach paired
    suspicion as part of the single-vote-decision guidance, since every role
    sees this body. Bare `相方` (actor mode) must not appear; only `相方候補`."""
    text = task_vote(["席1 A", "席2 B"], runoff=False)
    assert "相方候補" in text
    assert "2 人狼セット" in text
    # Existing baseline candidate list still present.
    assert "投票先として合法な候補は: 席1 A、席2 B" in text
    # Wolf-only block must not be appended.
    assert "仲間の人狼" not in text
    assert not re.search(r"相方(?!候補)", text)


def test_task_vote_wolf_block_extends_with_two_wolf_set_framing() -> None:
    """The wolf-only vote block must extend the existing partner checklist
    with a `2 人狼セット` line so the wolf checks each save/cut/deflect against
    next-day reading. Existing wolf-only tokens stay (`相方を救` / `身内票` /
    `ライン切り`)."""
    text = task_vote(
        ["席1 A", "席2 B"],
        runoff=False,
        role=Role.WEREWOLF,
        wolf_partner_tokens=["席2 B"],
    )
    assert "2 人狼セット" in text
    # Existing wolf-only checklist tokens preserved.
    assert "相方を救" in text
    assert "身内票" in text
    assert "ライン切り" in text


def test_task_vote_madman_inference_present_but_no_actor_partner() -> None:
    """The madman's vote text contains the common-path pair-inference clause
    (`相方候補`) but never the wolf-only actor verbs (`相方を救` / `相方を切`)
    or the partner-token block (`仲間の人狼`). Bare `相方` (actor mode) must
    not appear even when a caller naively passes a partner token."""
    text = task_vote(
        ["席1 A", "席2 B"],
        runoff=False,
        role=Role.MADMAN,
        wolf_partner_tokens=["席3 X"],
    )
    # Common-path pair-inference reaches the madman.
    assert "相方候補" in text
    # Wolf-only actor verbs and partner-token block must not.
    assert "仲間の人狼" not in text
    assert "相方を救" not in text
    assert "相方を切" not in text
    # Bare `相方` (actor mode) must be absent in the madman vote text.
    assert not re.search(r"相方(?!候補)", text)
