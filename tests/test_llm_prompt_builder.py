"""Tests for the prompt builder.

Pins down the safety invariants the rebuilt rules + strategy templates
must keep:

* The shared rules block states the canonical 9-player ruleset and the
  hard structural facts every LLM seat must respect (NIGHT_0 random
  white, attack-victim-non-wolf, knight no-self / no-consecutive,
  candidate-token strict match, day-1 fake-seer must claim white,
  per-target naming, public-claim immutability).
* The role-strategy block is role-scoped — wolf-coordination vocabulary
  (`相方` / `襲撃先を揃える`) appears only in the werewolf tips, and
  the madman tips prohibit (not assume) knowing real wolf positions.
* `build_system_prompt` and `build_user_context` glue the blocks
  together with the right placeholders filled and no leakage between
  roles.

Granular bullet-by-bullet content checks were dropped when the rules
file was rewritten — those couple the test suite too tightly to prose
phrasing. Tests here check structure, leakage, and the few invariants
the runtime depends on for safety.
"""

from __future__ import annotations

import pytest

from wolfbot.domain.enums import ROLE_JA, Phase, Role, SubmissionType
from wolfbot.domain.models import Game, Player, Seat
from wolfbot.llm.persona_base import JudgmentProfile, Persona, SpeechProfile
from wolfbot.llm.prompt_builder import (
    _build_game_rules_block,
    _build_judgment_profile_block,
    _build_speech_profile_block,
    _build_strategy_block,
    build_system_prompt,
    build_user_context,
    task_daytime_speech,
    task_night_action,
    task_vote,
    task_wolf_chat,
)
from wolfbot.npc.personas import NPC_PERSONAS_BY_KEY as PERSONAS_BY_KEY

# =========================================================================
# game-rules block — structural invariants
# =========================================================================


def test_game_rules_block_states_nine_player_village() -> None:
    block = _build_game_rules_block()
    assert "9 人村" in block
    assert "(合計 9)" in block


def test_game_rules_block_renders_role_distribution_from_constants() -> None:
    """The distribution sentence must list every role with its seat count
    derived from ROLE_DISTRIBUTION + ROLE_JA — not hard-coded text."""
    block = _build_game_rules_block()
    expected_pieces = [
        f"{ROLE_JA[Role.WEREWOLF]}2",
        f"{ROLE_JA[Role.MADMAN]}1",
        f"{ROLE_JA[Role.SEER]}1",
        f"{ROLE_JA[Role.MEDIUM]}1",
        f"{ROLE_JA[Role.KNIGHT]}1",
        f"{ROLE_JA[Role.VILLAGER]}3",
    ]
    for piece in expected_pieces:
        assert piece in block


def test_game_rules_block_states_win_conditions() -> None:
    block = _build_game_rules_block()
    assert "村勝利" in block or "村人陣営勝利" in block
    assert "狼勝利" in block or "人狼陣営勝利" in block
    # Madman is counted as non-wolf for the count, but the wolf side wins.
    assert "狂人" in block


# ---------------------- night-action structural facts ----------------------


def test_game_rules_block_states_night0_random_white_only() -> None:
    block = _build_game_rules_block()
    assert "NIGHT_0" in block
    # day 1 morning is structurally peaceful; cannot be inferred as GJ
    assert "GJ" in block or "護衛成功" in block
    assert "解釈してはいけない" in block or "構造ルール違反" in block


def test_game_rules_block_states_night0_random_white_can_be_madman() -> None:
    block = _build_game_rules_block()
    assert "狂人の可能性" in block


def test_game_rules_block_states_knight_no_self_no_consecutive() -> None:
    block = _build_game_rules_block()
    assert "自分護衛" in block
    assert "前夜と同じ対象護衛" in block or "連続護衛" in block


def test_game_rules_block_states_wolf_split_master_picks_one() -> None:
    block = _build_game_rules_block()
    assert "Master" in block
    assert "空振り" in block
    assert "ランダム" in block


# ---------------------- judgment color rules ----------------------


def test_game_rules_block_states_two_color_only() -> None:
    block = _build_game_rules_block()
    assert "黒 (本物の人狼)" in block
    assert "白 (本物の人狼ではない)" in block
    assert "2 値のみ" in block


def test_game_rules_block_states_madman_appears_white() -> None:
    block = _build_game_rules_block()
    assert "狂人は黒判定されない" in block


def test_game_rules_block_states_attack_victim_is_non_wolf_hard() -> None:
    """The 襲撃死=非狼 rule is the most safety-critical HARD fact: the
    listener-side must reject any 'attack victim was wolf' claim, and
    the speaker-side must never produce one."""
    block = _build_game_rules_block()
    assert "(襲撃)" in block
    assert "非狼確定" in block
    assert "HARD" in block


def test_game_rules_block_states_medium_white_is_not_village_confirmation() -> None:
    block = _build_game_rules_block()
    assert "本物の人狼ではない" in block
    # The legacy rule was that medium white doesn't identify role; the new
    # text expresses this as "役職までは特定しない".
    assert "役職" in block and "特定しない" in block


# ---------------------- public-claim immutability ----------------------


def test_game_rules_block_forbids_retroactive_judgment_change() -> None:
    block = _build_game_rules_block()
    assert "後のターンで対象・色・日付を絶対に書き換えない" in block


def test_game_rules_block_states_per_target_naming_required() -> None:
    block = _build_game_rules_block()
    assert "「全員白」" in block or "「すべて白」" in block
    assert "破綻" in block


def test_game_rules_block_states_day1_seer_only_one_white_result() -> None:
    block = _build_game_rules_block()
    # day 1 朝の占い結果: NIGHT_0 ランダム白 1 件のみ
    assert "NIGHT_0 ランダム白 1 件" in block
    assert "day 1 朝" in block
    # Black claim on day 1 is forbidden
    assert "黒を主張" in block


def test_game_rules_block_states_day1_medium_has_no_result() -> None:
    block = _build_game_rules_block()
    assert "day 1 朝の霊媒結果は存在しない" in block


def test_game_rules_block_states_seer_count_is_n_plus_one() -> None:
    block = _build_game_rules_block()
    assert "通算 N+1 件" in block


# ---------------------- candidate-token rule ----------------------


def test_game_rules_block_requires_candidate_token_strict_match() -> None:
    block = _build_game_rules_block()
    assert "席3 Alice" in block
    assert "完全一致" in block


# ---------------------- CO inference ----------------------


def test_game_rules_block_distinguishes_topical_co_from_self_declaration() -> None:
    """The classic 騙り誤読 trap — must distinguish someone *talking about*
    a role-CO from someone actually *naming themselves* as that role."""
    block = _build_game_rules_block()
    assert "「占いCOについて」" in block or "話題化" in block
    assert "本人が「私は占い師です」" in block


def test_game_rules_block_treats_day1_single_co_as_truthy() -> None:
    block = _build_game_rules_block()
    assert "単独 CO" in block
    assert "day 1 朝" in block
    # The "single CO is truth-bias by default" rule
    assert "真として扱う" in block or "真寄り" in block


def test_game_rules_block_rejects_sole_survivor_as_single_co() -> None:
    """対抗 CO 履歴があった役職で残存 CO が 1 人になった時点では
    『単独 CO だから真』とは扱わない (狼が情報役を残した可能性)."""
    block = _build_game_rules_block()
    assert "通算" in block
    assert "現在生存中の占い CO は 1 人" in block


def test_game_rules_block_states_co_count_caps() -> None:
    block = _build_game_rules_block()
    # 占い 4 / 霊媒 2 / 騎士 2 の上限
    assert "占い 4" in block
    assert "霊媒 2" in block
    assert "騎士 2" in block


def test_game_rules_block_states_overflow_sum_three_marks_non_co_white() -> None:
    """Sum of (CO count - 1) across 占/霊/騎 reaching 3 means the wolf
    side has filled out the occult-CO slots, so non-CO seats are
    village-side white-grade."""
    block = _build_game_rules_block()
    assert "超過分合計" in block or "対抗超過分" in block
    assert "3" in block
    assert "確白" in block


def test_game_rules_block_states_dead_seats_excluded_from_today_vote() -> None:
    block = _build_game_rules_block()
    assert "死亡席" in block
    assert "本日の処刑対象" in block or "vote target" in block


def test_game_rules_block_introduces_two_wolf_pair_inference() -> None:
    block = _build_game_rules_block()
    assert "相方候補" in block
    assert "推理用語" in block


# ---------------------- progression playbooks ----------------------


def test_game_rules_block_defines_3_1_2_2_2_1_1_2_formations() -> None:
    block = _build_game_rules_block()
    for formation in ("3-1", "2-2", "2-1", "1-2"):
        assert formation in block


def test_game_rules_block_includes_rope_calculation() -> None:
    block = _build_game_rules_block()
    assert "縄" in block
    assert "floor((生存数 - 1) / 2)" in block
    assert "9 人村開始時 4 縄" in block


# ---------------------- forbidden glossary terms ----------------------


def test_game_rules_block_no_longer_includes_term_glossary_section() -> None:
    """The compact rewrite drops the 30-term glossary (グレラン / グレスケ /
    鉄板護衛 / 変態護衛 / 捨て護衛 / 視点漏れ / SG / etc.) because the speech
    LLM can't use those terms in `text` anyway and the decision LLM
    already gets the structural facts elsewhere. This test catches an
    accidental restoration."""
    block = _build_game_rules_block()
    # A few tell-tale glossary headers from the old version
    assert "グレラン: グレーから各自" not in block
    assert "鉄板護衛: 真寄り情報役" not in block
    assert "変態護衛: セオリー上の本命" not in block
    assert "視点漏れ: ある役職視点" not in block


# =========================================================================
# strategy block — leakage & role-scope invariants
# =========================================================================


_ALL_ROLES = (
    Role.WEREWOLF,
    Role.MADMAN,
    Role.SEER,
    Role.MEDIUM,
    Role.KNIGHT,
    Role.VILLAGER,
)


def test_strategy_block_renders_for_every_role() -> None:
    for role in _ALL_ROLES:
        block = _build_strategy_block(role)
        assert block
        # Each role file starts with a "# 〜 戦略" markdown heading;
        # the loader strips it. The body should be plain bullets.
        assert not block.startswith("# ")


def test_werewolf_strategy_includes_partner_coordination_vocabulary() -> None:
    block = _build_strategy_block(Role.WEREWOLF)
    # The wolf knows its partner — vocabulary that only makes sense inside
    # the wolf seat.
    assert "人狼チャット" in block
    assert "襲撃先を 1 人に揃える" in block
    # GJ rebite rule
    assert "GJ" in block and "再噛み" in block


@pytest.mark.parametrize(
    "role",
    [Role.MADMAN, Role.SEER, Role.MEDIUM, Role.KNIGHT, Role.VILLAGER],
)
def test_non_wolf_strategy_excludes_partner_coordination_vocabulary(
    role: Role,
) -> None:
    """Wolf-only coordination concepts (人狼チャット, 襲撃先を1人に揃える, GJ
    rebite, 視点漏れ as a self-imposed rule) must not appear in any non-wolf
    role's strategy. The non-wolf side may still use `相方候補` as an
    inference-only term — it appears in 公開ログからの 2 人狼仮説 — so we
    only exclude vocabulary that names the actual partner relationship."""
    block = _build_strategy_block(role)
    assert "人狼チャット" not in block
    assert "襲撃先を 1 人に揃える" not in block
    assert "再噛み" not in block


def test_madman_strategy_prohibits_assuming_known_wolf_positions() -> None:
    """The madman is wolf-faction but doesn't know who the wolves are.
    The strategy must spell that out so the LLM doesn't fabricate
    inside knowledge."""
    block = _build_strategy_block(Role.MADMAN)
    assert "本物の人狼位置を知っている前提で話してはならない" in block


def test_madman_prohibition_does_not_leak_to_other_roles() -> None:
    """The madman-only prohibition phrase must stay in madman.md and
    nowhere else — accidental cross-import would re-frame other roles
    as 'you don't know who the wolves are' which is misleading for
    actual seer/medium players."""
    needle = "本物の人狼位置を知っている前提で話してはならない"
    for role in _ALL_ROLES:
        if role is Role.MADMAN:
            continue
        assert needle not in _build_strategy_block(role)


def test_villager_strategy_forbids_self_role_co() -> None:
    """Villagers must not pretend to have private info — including the
    common trap of self-claiming '村人CO / 素村CO'."""
    block = _build_strategy_block(Role.VILLAGER)
    assert "村人CO" in block or "素村CO" in block
    assert "信用を取ろうとしない" in block or "CO しても証明にはならない" in block


def test_seer_strategy_requires_co_and_results_at_first_speak() -> None:
    block = _build_strategy_block(Role.SEER)
    assert "発言の番が回ってきたら" in block
    assert "CO + 判定結果を発表" in block or "CO + 結果" in block


def test_medium_strategy_states_day1_morning_has_no_result() -> None:
    block = _build_strategy_block(Role.MEDIUM)
    assert "day 1 朝の霊媒結果は構造的に存在しない" in block


def test_knight_strategy_forbids_self_and_dead_target_guards() -> None:
    block = _build_strategy_block(Role.KNIGHT)
    assert "自分護衛" in block
    assert "死亡対象護衛" in block


@pytest.mark.parametrize("role", _ALL_ROLES)
def test_every_role_strategy_carries_attack_victim_non_wolf_hard_fact(
    role: Role,
) -> None:
    """The 襲撃死=非狼 rule is so safety-critical it's repeated in every
    role-specific block — tests catch accidental deletion. Wolf phrases
    it as a self-imposed silence rule (not to claim victims as wolves);
    other roles phrase it as a listener-side detection rule."""
    block = _build_strategy_block(role)
    assert "(襲撃)" in block
    assert "襲撃死=非狼" in block or "非狼確定" in block


# =========================================================================
# build_system_prompt — block composition
# =========================================================================


def _persona() -> Persona:
    """A minimal persona for system-prompt composition tests."""
    return Persona(
        key="test_p",
        display_name="🧪テスト",
        style_guide="淡々と理屈で詰める。",
        speech_profile=SpeechProfile(
            first_person="僕",
            self_reference_aliases=(),
            address_style="呼び捨て",
            sentence_style="短く言い切る",
            pause_style="間は置かない",
            signature_phrases=("……まあ",),
            forbidden_overuse=("無闇な敬語",),
            narration_mode=None,
        ),
        judgment_profile=JudgmentProfile(),
    )


def test_build_system_prompt_substitutes_every_placeholder() -> None:
    persona = _persona()
    prompt = build_system_prompt(
        persona=persona,
        role=Role.SEER,
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        task_text="<<TEST_TASK>>",
    )
    # No placeholder leaks unfilled.
    for marker in (
        "{game_rules_block}",
        "{persona_block}",
        "{judgment_profile_block}",
        "{speech_profile_block}",
        "{role_block}",
        "{strategy_block}",
        "{phase_block}",
        "{task_block}",
    ):
        assert marker not in prompt
    # Persona surfaces.
    assert "🧪テスト" in prompt
    assert "淡々と理屈で詰める" in prompt
    # Phase + day surfaces.
    assert "DAY_DISCUSSION" in prompt
    assert "day 1" in prompt
    # Task surfaces verbatim.
    assert "<<TEST_TASK>>" in prompt
    # Role surfaces using ROLE_JA mapping.
    assert ROLE_JA[Role.SEER] in prompt
    # Strategy + rules blocks both reach the prompt.
    assert "(襲撃)" in prompt  # rules block
    assert "対抗 CO" in prompt  # rules + seer strategy share this


def test_build_system_prompt_role_scope_only_emits_own_strategy() -> None:
    """Composing for one role must not splice in another role's strategy
    body — wolf coordination must not appear in seer's prompt etc."""
    persona = _persona()
    seer_prompt = build_system_prompt(
        persona=persona,
        role=Role.SEER,
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        task_text="",
    )
    # Wolf vocabulary is fenced to werewolf.md
    assert "襲撃先を揃える" not in seer_prompt
    # Madman-only prohibition is fenced to madman.md
    assert "本物の人狼位置を知っている前提で話してはならない" not in seer_prompt


# =========================================================================
# judgment / speech profile blocks — band rendering & leakage
# =========================================================================


def _persona_with_judgment(profile: JudgmentProfile) -> Persona:
    return Persona(
        key="band_p",
        display_name="バンド",
        style_guide="テスト用",
        speech_profile=SpeechProfile(
            first_person="私",
            self_reference_aliases=(),
            address_style="さん付け",
            sentence_style="標準",
            pause_style="標準",
            signature_phrases=(),
            forbidden_overuse=(),
            narration_mode=None,
        ),
        judgment_profile=profile,
    )


def test_judgment_profile_block_extreme_logical_persona() -> None:
    persona = _persona_with_judgment(
        JudgmentProfile(
            trust_hard_facts=1.0,
            trust_medium_facts=1.0,
            contrarian_bias=0.0,
            aggression=0.0,
            bandwagon_tendency=0.0,
        )
    )
    block = _build_judgment_profile_block(persona)
    assert "絶対視" in block
    assert "基本受け入れる" in block
    assert "多数派にあえて逆らわない" in block
    assert "慎重で疑い先を出すのが遅い" in block
    assert "単独行動を好み流れに乗らない" in block


def test_judgment_profile_block_neutral_persona_renders_mid_bands() -> None:
    persona = _persona_with_judgment(JudgmentProfile())
    block = _build_judgment_profile_block(persona)
    assert "標準" in block


def test_speech_profile_block_kukrushka_uses_silent_gesture() -> None:
    """Kukrushka is the only persona that narrates by gesture (silent_gesture
    mode) — the block must structurally differ from chatty personas (no
    一人称 line, gesture examples instead)."""
    persona = PERSONAS_BY_KEY["kukrushka"]
    block = _build_speech_profile_block(persona)
    assert "叙述モード" in block
    assert "一人称" not in block


@pytest.mark.parametrize(
    "key",
    [k for k in PERSONAS_BY_KEY if k != "kukrushka"],
)
def test_speech_profile_block_standard_personas_have_first_person(
    key: str,
) -> None:
    block = _build_speech_profile_block(PERSONAS_BY_KEY[key])
    assert "一人称" in block


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


def test_build_user_context_no_co_parser_summary_section() -> None:
    """The pre-digest CO parser sections (CO list / 盤面分類 / 役職推定メモ)
    must not appear in user_context. The LLM is expected to read raw
    PLAYER_SPEECH lines and apply CO-detection rules itself."""
    seats = [_ctx_seat(1, "Alice"), _ctx_seat(2, "Bob")]
    players = [_ctx_player(1), _ctx_player(2)]
    out = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[_ctx_speech("占い師CO", actor_seat=1)],
        private_logs=[],
    )
    assert "## CO・判定の機械整理" not in out
    assert "## 盤面分類" not in out
    assert "占い師CO: 席" not in out


def test_build_user_context_passes_player_speech_through_raw() -> None:
    seats = [_ctx_seat(1, "Alice"), _ctx_seat(2, "Bob")]
    players = [_ctx_player(1), _ctx_player(2)]
    speech = "セツの判定が気になる、人狼っぽい雰囲気がある"
    out = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[_ctx_speech(speech, actor_seat=2)],
        private_logs=[],
    )
    assert speech in out


def test_build_user_context_includes_rope_block() -> None:
    seats = [_ctx_seat(i, f"P{i}") for i in range(1, 10)]
    players = [_ctx_player(i) for i in range(1, 10)]
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
    assert "生存 9 人" in out
    # 9-alive → 4 ropes; village starts at 4 ropes.
    assert "4 縄" in out
    assert "9人村開始時は4縄" in out


def test_build_user_context_wolf_partner_block_only_for_wolves() -> None:
    """A wolf seat sees its partner; a non-wolf seat must not."""
    seats = [_ctx_seat(i, f"P{i}") for i in range(1, 4)]
    players = [
        _ctx_player(1, role=Role.WEREWOLF),
        _ctx_player(2, role=Role.WEREWOLF),
        _ctx_player(3, role=Role.SEER),
    ]
    wolf_view = build_user_context(
        game=_ctx_game(),
        me=players[0],
        my_seat=seats[0],
        seats=seats,
        players=players,
        public_logs=[],
        private_logs=[],
    )
    seer_view = build_user_context(
        game=_ctx_game(),
        me=players[2],
        my_seat=seats[2],
        seats=seats,
        players=players,
        public_logs=[],
        private_logs=[],
    )
    assert "## 仲間の人狼" in wolf_view
    assert "P2" in wolf_view  # partner shown
    assert "## 仲間の人狼" not in seer_view


def test_build_user_context_renders_deduced_facts_section_when_provided() -> None:
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
        deduced_facts_block="HARD: 席3 は人狼ではない (襲撃死)",
    )
    assert "## 公開情報からの確定/推測事実" in out
    assert "HARD: 席3 は人狼ではない" in out


def test_build_user_context_omits_deduced_facts_section_when_none() -> None:
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
    assert "## 公開情報からの確定/推測事実" not in out


# =========================================================================
# task templates — partner-block scoping, day-2 hint gating
# =========================================================================


def test_task_vote_baseline_unchanged_for_non_wolf() -> None:
    base = task_vote(["席1 Alice", "席2 Bob"], runoff=False)
    assert "席1 Alice" in base
    assert "仲間の人狼" not in base
    assert "決選投票" not in base


def test_task_vote_runoff_note_appears_when_runoff_true() -> None:
    out = task_vote(["席1 Alice", "席2 Bob"], runoff=True)
    assert "決選投票" in out


@pytest.mark.parametrize(
    "role",
    [Role.SEER, Role.MEDIUM, Role.KNIGHT, Role.VILLAGER, Role.MADMAN],
)
def test_task_vote_non_wolf_role_never_emits_partner_block(role: Role) -> None:
    """Even when a partner_tokens kwarg is forced for a non-wolf role
    (e.g. madman), the wolf-block must not render — partner identity
    must never reach non-wolf prompts."""
    out = task_vote(
        ["席1 Alice", "席2 Bob"],
        runoff=False,
        role=role,
        wolf_partner_tokens=("席3 Carol",),
    )
    assert "仲間の人狼" not in out
    assert "席3 Carol" not in out


def test_task_vote_wolf_emits_partner_checklist() -> None:
    out = task_vote(
        ["席1 Alice", "席2 Bob"],
        runoff=False,
        role=Role.WEREWOLF,
        wolf_partner_tokens=("席3 Carol",),
    )
    assert "仲間の人狼" in out
    assert "席3 Carol" in out
    assert "身内票" in out or "ライン切り" in out


def test_task_vote_wolf_runoff_adds_runoff_block() -> None:
    out = task_vote(
        ["席1 Alice", "席2 Bob"],
        runoff=True,
        role=Role.WEREWOLF,
        wolf_partner_tokens=("席3 Carol",),
    )
    assert "PP/RPP" in out


def test_task_vote_lone_wolf_returns_base_text() -> None:
    """Empty partner_tokens → wolf gets the same prompt as villagers."""
    out = task_vote(
        ["席1 Alice", "席2 Bob"],
        runoff=False,
        role=Role.WEREWOLF,
        wolf_partner_tokens=(),
    )
    assert "仲間の人狼" not in out


def test_task_daytime_speech_day2_round1_includes_results_hint() -> None:
    out = task_daytime_speech(day_number=2, discussion_round=1)
    assert "前夜の能力結果" in out


def test_task_daytime_speech_day1_round1_wolf_madman_branch() -> None:
    out = task_daytime_speech(day_number=1, discussion_round=1, role=Role.WEREWOLF)
    assert "占い師騙り・霊媒師騙り・潜伏" in out


def test_task_daytime_speech_day1_round1_villager_no_wolf_block() -> None:
    out = task_daytime_speech(day_number=1, discussion_round=1, role=Role.VILLAGER)
    assert "占い師騙り・霊媒師騙り・潜伏" not in out


@pytest.mark.parametrize(
    "kind,label",
    [
        (SubmissionType.WOLF_ATTACK, "襲撃"),
        (SubmissionType.SEER_DIVINE, "占い"),
        (SubmissionType.KNIGHT_GUARD, "護衛"),
    ],
)
def test_task_night_action_uses_kind_specific_label(kind: SubmissionType, label: str) -> None:
    out = task_night_action(kind, ["席1 Alice", "席2 Bob"])
    assert label in out
    assert "席1 Alice" in out


def test_task_night_action_wolf_only_advice_is_kind_scoped() -> None:
    """Wolf-attack value scoring lives only in WOLF_ATTACK; knight tradeoffs
    only in KNIGHT_GUARD; seer value only in SEER_DIVINE."""
    wolf = task_night_action(SubmissionType.WOLF_ATTACK, ["席1 X"])
    knight = task_night_action(SubmissionType.KNIGHT_GUARD, ["席1 X"])
    seer = task_night_action(SubmissionType.SEER_DIVINE, ["席1 X"])
    assert "騎士探し" in wolf and "騎士探し" not in knight and "騎士探し" not in seer
    assert "鉄板護衛" in knight and "鉄板護衛" not in wolf
    assert "占い価値" in seer and "占い価値" not in wolf


def test_task_wolf_chat_lists_partners_and_candidates() -> None:
    out = task_wolf_chat(["席3 Carol"], ["席1 Alice", "席2 Bob"])
    assert "席3 Carol" in out
    assert "席1 Alice" in out
    assert "襲撃" in out
