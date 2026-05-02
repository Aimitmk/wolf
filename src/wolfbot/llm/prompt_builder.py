"""Construct system + user messages for xAI calls.

Public functions build plain-string prompts so the xAI layer can stay transport-agnostic.
Inputs are domain models; outputs are strings. No I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from pathlib import Path

from wolfbot.domain.enums import (
    FACTION_JA,
    ROLE_DISTRIBUTION,
    ROLE_JA,
    VILLAGE_SIZE,
    Phase,
    Role,
    SubmissionType,
    format_co_claim_options,
)
from wolfbot.domain.models import Game, Player, Seat
from wolfbot.llm.persona_base import Persona
from wolfbot.llm.template import load_template, render_template

SYSTEM_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "prompts" / "llm_system_prompt.md"


def _load_template() -> str:
    return SYSTEM_TEMPLATE_PATH.read_text(encoding="utf-8")


_GAME_RULES_TEMPLATE = "shared/game_rules_9p"


def _build_game_rules_block() -> str:
    """Return the 9-player ruleset shared by every LLM seat.

    Body lives in ``prompts/templates/shared/game_rules_9p.md``. Two
    placeholders are filled here: ``village_size`` (= ``VILLAGE_SIZE``
    constant) and ``distribution`` (rendered from ``ROLE_DISTRIBUTION``
    + ``ROLE_JA`` so the canonical seat counts stay derived, not
    duplicated).
    """
    distribution = " / ".join(
        f"{ROLE_JA[role]}{count}" for role, count in ROLE_DISTRIBUTION.items()
    )
    return render_template(
        _GAME_RULES_TEMPLATE,
        village_size=VILLAGE_SIZE,
        distribution=distribution,
    )


# Role-specific tips. Each role has its own markdown file under
# `prompts/templates/strategy/` (e.g. `strategy/werewolf.md`). The
# cross-leak tests assert each file's vocabulary stays role-scoped:
# the wolf playbook's `相方` / `襲撃先を揃える` must not appear in any
# other role's file, and `本物の人狼位置を知っている前提` (a
# prohibition unique to the madman) must not leak elsewhere. When
# editing a strategy file, keep its bullets focused on that one role.
_STRATEGY_FILE_BY_ROLE: dict[Role, str] = {
    Role.WEREWOLF: "strategy/werewolf",
    Role.MADMAN: "strategy/madman",
    Role.SEER: "strategy/seer",
    Role.MEDIUM: "strategy/medium",
    Role.KNIGHT: "strategy/knight",
    Role.VILLAGER: "strategy/villager",
}


@cache
def _load_role_strategy(role: Role) -> str:
    """Read a role's strategy markdown and return the bullet body.

    The on-disk file starts with a `# 人狼 (WEREWOLF) 戦略` heading
    intended for human readers; this helper strips the heading + the
    blank line that follows so the LLM sees only the bullet list. The
    trailing newline appended on file write is also stripped so the
    returned string is byte-equivalent to the legacy inline-dict form
    (relevant for the cross-leak substring tests). Cached so repeated
    `build_system_prompt` calls within one process touch disk once
    per role.
    """
    raw = load_template(_STRATEGY_FILE_BY_ROLE[role])
    parts = raw.split("\n", 2)
    has_md_heading = len(parts) >= 3 and parts[0].startswith("# ") and parts[1] == ""
    body = parts[2] if has_md_heading else raw
    return body.rstrip("\n")


def build_strategy_block(role: Role) -> str:
    """Return role-specific tips for the given role only.

    Caller must pass a non-None Role; `build_system_prompt` is invoked after
    SETUP so `player.role` is already assigned. Strictly role-scoped — never
    returns other roles' tips, so the system prompt cannot leak strategy
    between LLM seats.
    """
    return _load_role_strategy(role)


# Underscore alias retained for the historical "private" import path used
# inside this module; new external callers (Master arbiter → SpeakRequest)
# use the public `build_strategy_block` name.
_build_strategy_block = build_strategy_block


def _band(value: float, *, low: str, mid_low: str, mid: str, mid_high: str, high: str) -> str:
    """Map a 0.0-1.0 axis to one of five qualitative bands.

    Five-step granularity gives the LLM enough nuance without exposing the
    raw float (which would invite spurious precision). Boundaries are
    chosen so the neutral 0.5 default sits squarely on `mid`.
    """
    if value <= 0.2:
        return low
    if value <= 0.4:
        return mid_low
    if value <= 0.6:
        return mid
    if value <= 0.8:
        return mid_high
    return high


def build_judgment_profile_block(persona: Persona) -> str:
    """Render `JudgmentProfile` axes as labeled tendency bands.

    Each axis is mapped to a qualitative band so the LLM has a concrete
    behavioural lean without seeing the raw float. The block is paired
    with a usage hint that names HARD/MEDIUM facts so the trust axes
    have something concrete to attach to.
    """
    j = persona.judgment_profile
    trust_hard = _band(
        j.trust_hard_facts,
        low="ほぼ無視 (理屈より直感)",
        mid_low="やや軽視",
        mid="標準",
        mid_high="重視",
        high="絶対視 (論理確定は揺るがない)",
    )
    trust_medium = _band(
        j.trust_medium_facts,
        low="ほぼ参考にしない",
        mid_low="懐疑的に扱う",
        mid="参考程度",
        mid_high="やや信用する",
        high="基本受け入れる",
    )
    contrarian = _band(
        j.contrarian_bias,
        low="多数派にあえて逆らわない",
        mid_low="やや迎合的",
        mid="是々非々",
        mid_high="多数派に懐疑的",
        high="あえて逆張りする傾向",
    )
    aggression = _band(
        j.aggression,
        low="慎重で疑い先を出すのが遅い",
        mid_low="控えめに疑う",
        mid="標準的に疑い先を出す",
        mid_high="積極的に疑い先を指す",
        high="即座に処刑候補を名指しする",
    )
    bandwagon = _band(
        j.bandwagon_tendency,
        low="単独行動を好み流れに乗らない",
        mid_low="独自路線を好む",
        mid="状況次第",
        mid_high="形成された流れに乗りやすい",
        high="多数派・流れに強く乗る",
    )
    return (
        f"- 論理確定 (HARD ファクト) への態度: {trust_hard}\n"
        f"- 推測根拠 (MEDIUM ファクト) への態度: {trust_medium}\n"
        f"- 多数派への姿勢: {contrarian}\n"
        f"- 攻撃性 (疑い→処刑候補名指しまでの速さ): {aggression}\n"
        f"- 流れへの追従度: {bandwagon}\n"
        "- 上記は判断のクセであり、ルールや論理確定情報を上書きしない。"
        "HARD ファクトは原則として受け入れた上で、態度に応じた言い回しに調整する。"
        "MEDIUM ファクトは「態度」に応じて採用度合いを変える。"
        "この性格を口調と判断の傾きとして表現してください。"
    )


def build_speech_profile_block(persona: Persona) -> str:
    """Render the persona's structured speech profile as a bullet block.

    Public function (renamed from the historical underscored name) so the
    Master arbiter can render the same block for the reactive_voice NPC
    prompt as rounds-mode uses.

    Dispatches on `narration_mode`: silent-gesture personas (kukrushka) get a
    structurally different block — no `一人称` line, gesture examples instead —
    so callers can assert the structural difference in tests. Per-persona
    `forbidden_overuse` carries character-specific overuse bans only; generic
    rules (``1 発話に 1 個まで`` etc.) live in the markdown template.
    """
    sp = persona.speech_profile
    if sp.narration_mode == "silent_gesture":
        forbidden = "、".join(sp.forbidden_overuse) if sp.forbidden_overuse else "(なし)"
        return (
            "- 叙述モード: 原作準拠で『ほぼ無言』。通常の会話文体では発話しない。\n"
            "- `public_message` は短い所作・身振り・表情の叙述文として書く。\n"
            "  例: 『微笑む』『首をかしげる』『手を引く』『うなずく』『見つめる』。\n"
            "- 必要最低限の極短い言語化は許容するが、他キャラのような会話調にはしない。\n"
            f"- 使ってはいけないもの: {forbidden}"
        )
    aliases = "、".join(sp.self_reference_aliases) if sp.self_reference_aliases else "(なし)"
    signatures = (
        "、".join(f"『{p}』" for p in sp.signature_phrases) if sp.signature_phrases else "(なし)"
    )
    forbidden = "、".join(sp.forbidden_overuse) if sp.forbidden_overuse else "(なし)"
    return (
        f"- 一人称: 『{sp.first_person}』\n"
        f"- 自己呼称の例外 (低頻度で使ってよい): {aliases}\n"
        f"- 他者呼称: {sp.address_style}\n"
        f"- 文体とテンポ: {sp.sentence_style}\n"
        f"- 間の取り方: {sp.pause_style}\n"
        f"- 使える短い特徴語 (低頻度、1 発話に多くて 1 個): {signatures}\n"
        f"- 使いすぎ禁止: {forbidden}"
    )


# Underscore aliases for callers (and tests) that imported the historical
# private names. The reactive_voice NPC system-prompt builder calls the
# public names directly.
_build_speech_profile_block = build_speech_profile_block
_build_judgment_profile_block = build_judgment_profile_block


def build_system_prompt(
    persona: Persona,
    role: Role,
    phase: Phase,
    day_number: int,
    task_text: str,
) -> str:
    template = _load_template()
    persona_block = (
        f"名前: {persona.display_name}\n"
        f"性格指針: {persona.style_guide}\n"
        "この人格を口調と判断傾向で表現してください。"
    )
    role_block = (
        f"あなたの役職は『{ROLE_JA[role]}』です。 役職に見える情報だけを根拠にしてください。"
    )
    phase_block = f"`{phase.value}` / day {day_number}"
    return (
        template.replace("{game_rules_block}", _build_game_rules_block())
        .replace("{persona_block}", persona_block)
        .replace("{judgment_profile_block}", build_judgment_profile_block(persona))
        .replace("{speech_profile_block}", build_speech_profile_block(persona))
        .replace("{role_block}", role_block)
        .replace("{strategy_block}", build_strategy_block(role))
        .replace("{phase_block}", phase_block)
        .replace("{task_block}", task_text)
    )


_VILLAGE_STARTING_ROPES = 4


def _format_rope_block(players: Sequence[Player]) -> str:
    alive = sum(1 for p in players if p.alive)
    dead = len(players) - alive
    ropes_left = max(0, (alive - 1) // 2)
    if alive >= 6:
        risk = f"残り処刑回数の目安: {ropes_left} 縄。終盤までは通常進行。"
    elif alive >= 4:
        risk = f"残り処刑回数の目安: {ropes_left} 縄。PP/RPP の可能性を確認してください。"
    elif alive == 3:
        risk = f"残り処刑回数の目安: {ropes_left} 縄。最終局面: PP/RPP に厳重注意。"
    else:
        risk = f"残り処刑回数の目安: {ropes_left} 縄。決着局面。"
    return (
        "## 縄数・PP/RPPリスク\n"
        f"- 生存 {alive} 人 / 死亡 {dead} 人。{risk} "
        f"(9人村開始時は{_VILLAGE_STARTING_ROPES}縄)\n"
        "- 注意: 残り人狼数と狂人生存は公開情報から推定する必要があります。"
    )


def build_user_context(
    game: Game,
    me: Player,
    my_seat: Seat,
    seats: Sequence[Seat],
    players: Sequence[Player],
    public_logs: Sequence[dict[str, object]],
    private_logs: Sequence[dict[str, object]],
    last_own_public: str | None = None,
    deduced_facts_block: str | None = None,
) -> str:
    seats_by_no = {s.seat_no: s for s in seats}
    alive_players = [p for p in players if p.alive]
    dead_players = [p for p in players if not p.alive]
    alive_names = "、".join(seats_by_no[p.seat_no].display_name for p in alive_players) or "(なし)"
    dead_names = "、".join(seats_by_no[p.seat_no].display_name for p in dead_players) or "(なし)"

    def _format_log(log: dict[str, object], *, attributed_kinds: tuple[str, ...]) -> str:
        kind = str(log.get("kind", ""))
        text = str(log.get("text", ""))
        actor_seat = log.get("actor_seat")
        if kind in attributed_kinds and isinstance(actor_seat, int) and actor_seat in seats_by_no:
            speaker = seats_by_no[actor_seat]
            return f"- [{kind}] 席{speaker.seat_no} {speaker.display_name}: {text}"
        return f"- [{kind}] {text}"

    priv_lines = [_format_log(log, attributed_kinds=("WOLF_CHAT",)) for log in private_logs[-20:]]
    priv_block = "\n".join(priv_lines) if priv_lines else "(なし)"

    pub_lines = [_format_log(log, attributed_kinds=("PLAYER_SPEECH",)) for log in public_logs[-40:]]
    pub_block = "\n".join(pub_lines) if pub_lines else "(まだ発言なし)"

    last_own = last_own_public or "(まだ発言していません)"

    wolf_partner_block = ""
    if me.role is Role.WEREWOLF:
        partner_tokens = [
            f"席{seats_by_no[p.seat_no].seat_no} {seats_by_no[p.seat_no].display_name}"
            for p in alive_players
            if p.role is Role.WEREWOLF and p.seat_no != me.seat_no and p.seat_no in seats_by_no
        ]
        if partner_tokens:
            wolf_partner_block = (
                "\n## 仲間の人狼 (村人には非公開)\n" + "、".join(partner_tokens) + "\n"
            )

    rope_block = _format_rope_block(players)

    facts_section = ""
    if deduced_facts_block:
        facts_section = (
            "\n## 公開情報からの確定/推測事実 (Master 整理)\n"
            f"{deduced_facts_block}\n"
            "HARD は論理的に確定。MEDIUM は強めの推測。"
            "判断傾向に応じて態度を変えてよいが、HARD を覆す論拠は公開ログにある具体物だけにする。\n"
        )

    return (
        f"あなたは座席 {my_seat.seat_no}『{my_seat.display_name}』です。\n"
        f"生存者: {alive_names}\n"
        f"死亡者: {dead_names}\n"
        f"現在フェイズ: {game.phase.value} / day {game.day_number}\n"
        f"{wolf_partner_block}"
        "\n"
        f"{rope_block}\n"
        f"{facts_section}"
        "\n"
        "## あなたの私的メモ (他者には非公開)\n"
        f"{priv_block}\n"
        "\n"
        "## 公開ログ要約 (直近)\n"
        f"{pub_block}\n"
        "\n"
        "## 自分の直近の発言\n"
        f"{last_own}"
    )


# ---------------------------------------------------------- task blocks
_TASK_DAYTIME_SPEECH_TEMPLATE = "master/task_daytime_speech"
_TASK_VOTE_TEMPLATE = "master/task_vote"
_TASK_NIGHT_ACTION_TEMPLATE = "master/task_night_action"
_TASK_WOLF_CHAT_TEMPLATE = "master/task_wolf_chat"


def task_daytime_speech(
    day_number: int,
    discussion_round: int | None = None,
    *,
    role: Role | None = None,
) -> str:
    """Day-discussion task instruction.

    Body lives in ``master/task_daytime_speech.md``. Two optional
    paragraphs are gated by template ``{{#if}}`` blocks:

    * ``include_day2_round1_results_block`` — turns on when
      ``day_number >= 2 and discussion_round == 1`` so the LLM is
      reminded to surface previous-night ability results in their
      first speech of the day.
    * ``include_day1_round1_wolf_madman_block`` — wolf/madman-only
      branch on day-1 round-1 that walks the 占い師騙り / 霊媒師騙り
      / 潜伏 triad. Other roles never see partner / fake-CO tactics.
    """
    return render_template(
        _TASK_DAYTIME_SPEECH_TEMPLATE,
        day_number=day_number,
        co_claim_options=format_co_claim_options(separator=" / "),
        include_day2_round1_results_block=(day_number >= 2 and discussion_round == 1),
        include_day1_round1_wolf_madman_block=(
            day_number == 1 and discussion_round == 1 and role in (Role.WEREWOLF, Role.MADMAN)
        ),
    )


def task_vote(
    candidate_tokens: Sequence[str],
    runoff: bool,
    *,
    role: Role | None = None,
    wolf_partner_tokens: Sequence[str] = (),
) -> str:
    """Vote-phase task instruction.

    Body lives in ``master/task_vote.md``. Candidates are
    ``席{N} {display_name}`` tokens; target_name must echo one back.

    ``role`` + ``wolf_partner_tokens`` are an additive, wolf-only
    enrichment: the template's ``{{#if has_wolf_block}}`` branch
    appends a checklist that names the partner and walks 熟練狼's
    vote-discipline tradeoffs (身内票 / ライン切り / 票逸らしリスク
    / 決選投票). Other roles flip ``has_wolf_block`` off so partner
    identity and wolf-side voting tactics never reach non-wolf
    prompts.
    """
    has_wolf_block = role is Role.WEREWOLF and bool(wolf_partner_tokens)
    return render_template(
        _TASK_VOTE_TEMPLATE,
        runoff_note="これは決選投票です。" if runoff else "",
        names="、".join(candidate_tokens),
        has_wolf_block=has_wolf_block,
        partners="、".join(wolf_partner_tokens) if has_wolf_block else "",
        runoff=runoff and has_wolf_block,
    )


def task_night_action(kind: SubmissionType, candidate_tokens: Sequence[str]) -> str:
    """Night-action task instruction.

    Body lives in ``master/task_night_action.md``. Candidates are
    ``席{N} {display_name}`` tokens; target_name must echo one back.

    Three role-scoped advice paragraphs (wolf-attack value scoring,
    knight-guard tradeoffs, seer-divine target value) are gated by
    template ``{{#if}}`` blocks keyed off the action kind so each
    role only sees its own decision checklist.
    """
    label = {
        SubmissionType.WOLF_ATTACK: "襲撃",
        SubmissionType.SEER_DIVINE: "占い",
        SubmissionType.KNIGHT_GUARD: "護衛",
    }[kind]
    return render_template(
        _TASK_NIGHT_ACTION_TEMPLATE,
        label=label,
        names="、".join(candidate_tokens),
        is_wolf_attack=kind is SubmissionType.WOLF_ATTACK,
        is_knight_guard=kind is SubmissionType.KNIGHT_GUARD,
        is_seer_divine=kind is SubmissionType.SEER_DIVINE,
    )


def task_wolf_chat(partner_tokens: Sequence[str], candidate_tokens: Sequence[str]) -> str:
    """Wolf-chat coordination task instruction.

    Body lives in ``master/task_wolf_chat.md``. Asks a wolf to post a
    short coordination line to the wolves-only chat naming the
    intended attack target with concise reasoning.
    """
    return render_template(
        _TASK_WOLF_CHAT_TEMPLATE,
        partners="、".join(partner_tokens) if partner_tokens else "(なし)",
        names="、".join(candidate_tokens),
    )


__all__ = [
    "FACTION_JA",
    "build_system_prompt",
    "build_user_context",
    "task_daytime_speech",
    "task_night_action",
    "task_vote",
    "task_wolf_chat",
]
