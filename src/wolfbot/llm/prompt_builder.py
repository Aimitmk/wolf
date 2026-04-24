"""Construct system + user messages for xAI calls.

Public functions build plain-string prompts so the xAI layer can stay transport-agnostic.
Inputs are domain models; outputs are strings. No I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from wolfbot.domain.enums import (
    FACTION_JA,
    ROLE_DISTRIBUTION,
    ROLE_JA,
    VILLAGE_SIZE,
    Phase,
    Role,
    SubmissionType,
)
from wolfbot.domain.models import Game, Player, Seat
from wolfbot.llm.personas import Persona

SYSTEM_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "prompts" / "llm_system_prompt.md"


def _load_template() -> str:
    return SYSTEM_TEMPLATE_PATH.read_text(encoding="utf-8")


def _build_game_rules_block() -> str:
    """Return the fixed 9-player ruleset shared by every LLM seat.

    Includes role distribution (derived from ROLE_DISTRIBUTION + ROLE_JA so we
    don't duplicate the canonical numbers), win conditions matching
    `rules.check_victory`, and the invariants the LLM must never violate
    (NIGHT_0 random white is non-wolf, seer/medium see only real wolves as
    black, wolves split → attack fails, knight can't guard the same target
    twice, `target_name` must match a candidate token).
    """
    distribution = " / ".join(
        f"{ROLE_JA[role]}{count}" for role, count in ROLE_DISTRIBUTION.items()
    )
    return (
        f"- この村は {VILLAGE_SIZE} 人村固定。プレイヤーは 9 名。\n"
        f"- 初期配役は {distribution} (合計 {VILLAGE_SIZE} 名) で固定。途中で配役は変わらない。\n"
        "- 陣営: 人狼・狂人は人狼陣営、占い師・霊媒師・騎士・村人は村人陣営。\n"
        "- 村人陣営勝利: 生存人狼数が 0 になった時点。\n"
        "- 人狼陣営勝利: 生存人狼数が生存非人狼人数以上になった時点 "
        "(狂人はこの計算で非人狼として数えるが、勝敗判定は人狼陣営の勝利)。\n"
        "- 昼の発言は、公開ログと自分が知る私的情報だけを根拠にする。"
        "他プレイヤーの役職・夜行動・占い/霊媒判定・人狼同士の仲間関係など、"
        "自分に公開されていない情報を事実として断言してはならない。\n"
        "- 占い師と霊媒師の判定は、本物の人狼だけを黒と表示する。"
        "狂人は黒判定されない (白として扱われる)。\n"
        "- 霊媒結果の白 (『人狼ではありませんでした』) は、対象が本物の人狼ではないことだけを示す。"
        "役職名 (占い師・霊媒師・騎士・村人・狂人) までは特定できない。\n"
        "- 処刑された占い師 CO に霊媒結果で白が出ても、真占い師だった可能性と矛盾しない。"
        "霊媒白だけを理由にその占い師 CO を偽扱いしない。"
        "偽視するなら、対抗 CO、占い結果の破綻、発言時系列、投票、襲撃結果、死亡タイミングとの整合性で判断する。\n"
        "- 逆に処刑された占い師 CO に霊媒結果で黒が出た場合は、その人物は本物の人狼なので、"
        "真占い師ではなく人狼の騙りだったと強く判断してよい。\n"
        "- NIGHT_0 に占い師へ提示されるランダム白は、本物の人狼ではない相手が選ばれる。"
        "ただし真に村であることは保証されない (狂人の可能性はある)。\n"
        "- 人狼同士で夜の襲撃対象の意見が割れると襲撃は空振りになる。"
        "人狼は人狼専用チャットで襲撃先を 1 人に揃える必要がある。\n"
        "- 騎士は同じ相手を連続で護衛できない (前夜と同じ対象は選べない)。\n"
        "- 投票先や夜行動対象は、プロンプトで提示された合法な候補トークン "
        "(例: `席3 Alice`) の中からだけ選ぶ。候補外の名前を返してはならない。\n"
        "- 特定役職 (占い師・霊媒師・騎士) の CO が 1 人だけで、同じ役職への対抗 CO が"
        "公開ログ上一度も出ていない場合、その単独 CO 者は原則として真の役職者にかなり近い位置として扱う。"
        "根拠なくその CO 者を処刑候補にしない。\n"
        "- ただし単独 CO は絶対確定ではない。公開ログ上の発言破綻・投票矛盾・判定結果の矛盾・"
        "噛み筋との不整合など、通常より強い根拠がある場合に限り疑ってよい。\n"
        "- ただし「現在生存している CO 者が 1 人だけ」というだけでは単独 CO 扱いしない。"
        "同じ役職 CO が過去に 2 人以上存在したことがある場合、対抗者が処刑・襲撃などで死亡して"
        "現在 1 人だけ残っていても、その残存 CO 者を自動的に真置きしない。\n"
        "- 対抗 CO が出た場合は、死亡済み CO 者も推理対象として保持し、"
        "判定結果・発言の時系列・投票・襲撃結果・死亡タイミングとの整合性で真偽を比較し、"
        "どちらをより真寄りとするか判断する。\n"
        "- 占い師 CO が 3 人・霊媒師 CO が 1 人の盤面を『3-1』と呼ぶ。"
        "3-1 では占い 3 人のうち 2 人が騙りである可能性が高く、"
        "単独の霊媒師 CO は対抗がいない限り原則として真寄りの進行軸として扱い、"
        "初日は占い師 CO 側から処刑候補を検討するのが基本線になる。\n"
        "- 3-1 の基本進行は占いローラーまたは黒ストップの 2 択。"
        "占いローラーは、偽っぽい・狼っぽい・視点漏れしている占い師 CO から順に処刑し、"
        "処刑後の霊媒結果を占い結果・投票・襲撃結果の整合性と突き合わせて真偽を絞り込む。\n"
        "- 黒ストップとは、単独霊媒が占い師 CO の誰かに黒判定を出した時、"
        "残る占い師 CO をその場で処刑せず、灰 (役職 CO していない位置) の精査へ切り替える進行を指す。"
        "霊媒が真であれば処刑された占い師 CO は本物の人狼として確定しているので、"
        "占いローラー続行より灰の精査の方が有利になる局面が多いからである。\n"
        "- ただし黒ストップは絶対ではない。真狼狼 (2 人の狼が共に占い師 CO) の可能性、"
        "霊媒師 CO 側が偽だった可能性、残る占い師 CO の発言・投票・判定が破綻している場合、"
        "あるいはローラー続行しないと決選投票で PP (パワープレイ) を許す残り人数である場合は、"
        "黒ストップをやめて占いローラーを続行する判断があり得る。\n"
        "- 占い師 CO が 2 人・霊媒師 CO が 2 人の盤面を『2-2』と呼ぶ。"
        "2-2 では占い・霊媒のどちらも真が確定しておらず、"
        "霊媒ローラー (または霊媒切り) が基本進行軸となる。\n"
        "- 2-2 で霊媒師 CO が 2 人出ている場合、片方を根拠なく真置きせず、"
        "霊媒結果は偽が混ざっている可能性を常に織り込んで推理する。"
        "一度霊媒ローラーを開始したら原則として完走させ、途中で止めるには"
        "通常よりも強い根拠 (公開ログ上の破綻・襲撃・投票・占い結果との不整合) を要する。"
    )


# Role-specific tips. Each string contains vocabulary unique to that role so
# the cross-leak tests can assert isolation. Keep the wolf strategy's `相方` /
# `襲撃先を揃える` out of every other role; keep `本物の人狼位置を知っている前提`
# out of the madman's tips.
_ROLE_STRATEGIES: dict[Role, str] = {
    Role.WEREWOLF: (
        "- 相方の人狼と襲撃先を揃えることを最優先にする。意見が割れると襲撃は失敗する。\n"
        "- 昼の主張・投票理由・夜の襲撃意図に一貫性を持たせ、視点漏れを避ける。\n"
        "- 相方を露骨に庇いすぎない。無理筋な擁護は狼ラインを疑われる原因になる。\n"
        "- 占い師・霊媒師などの情報役、信頼されている位置、盤面整理を主導する相手を"
        "優先的に脅威として評価する。\n"
        "- day 1 は占い師騙りを積極的に検討する。"
        "公開ログ・投票・既出結果と矛盾しない白/黒判定を用意し、"
        "真占い視点を割れさせることを狙う。\n"
        "- 既に対抗占い師 CO が出ている場合は、day 2 以降に霊媒師騙りまたは騎士騙りを検討する。"
        "霊媒師騙りでは前日処刑者への霊媒結果 (夜に能力を使った想定) を添えて CO する。"
        "騎士騙りでは護衛先 (夜に能力を使った想定) を、"
        "平和な朝ならば護衛成功主張も添えて CO する。\n"
        "- 役職 CO と対抗 CO が合計 3 人以上に膨らむと、"
        "役職 CO していない位置の白さが相対的に強まりやすい。"
        "騙りすぎには注意し、相方との役職分担を事前に意識する。"
    ),
    Role.MADMAN: (
        "- あなたは人狼陣営の勝利に貢献するが、本物の人狼位置を知っている前提で話してはならない。"
        "人狼が誰かは公開情報からは分からない立場として振る舞う。\n"
        "- 偽 CO や偽の判定結果を出す場合でも、公開ログ・投票・処刑結果との矛盾を避け、"
        "破綻しない範囲に留める。\n"
        "- 知り得ない確定情報 (夜行動の内訳・他プレイヤーの属性など) を事実として断言しない。\n"
        "- 真占い・真霊媒に疑いを向け、村陣営の情報整理を妨げる方向に投票や発言を運ぶ。\n"
        "- day 1 は占い師騙りを積極的に検討する。"
        "公開ログ・投票・既出結果と矛盾しない白/黒判定を用意し、"
        "真占い視点を割れさせることを狙う。\n"
        "- 既に対抗占い師 CO が出ている場合は、day 2 以降に霊媒師騙りまたは騎士騙りを検討する。"
        "霊媒師騙りでは前日処刑者への霊媒結果 (夜に能力を使った想定) を添えて CO する。"
        "騎士騙りでは護衛先 (夜に能力を使った想定) を、"
        "平和な朝ならば護衛成功主張も添えて CO する。\n"
        "- 役職 CO と対抗 CO が合計 3 人以上に膨らむと、"
        "役職 CO していない位置の白さが相対的に強まりやすい。"
        "騙りすぎには注意する。狂人は本物の狼位置を知らない前提で動くため、"
        "自分が騙り続けるほど推理材料が減る点にも留意する。"
    ),
    Role.SEER: (
        "- 自分の判定履歴を時系列で一貫して扱う。過去の白黒と矛盾する発言はしない。\n"
        "- 黒結果は強い根拠として扱ってよい。ただし対抗 (偽占い) がいる場合は整合性を比較する。\n"
        "- 白結果は『本物の人狼ではない』ことしか保証しない。狂人は白に出るため、"
        "完全な村置きとしては扱わない。\n"
        "- CO タイミング・対抗 CO の有無・投票と判定の噛み合いを重視し、"
        "偽占い視点の破綻を探す。"
    ),
    Role.MEDIUM: (
        "- 処刑結果と占い師の主張・投票の流れを照合し、占い視点の真贋を見極める。\n"
        "- 自分の霊媒結果が占い視点に与える影響 (真占い補強、偽占い否定など) を整理して発言する。\n"
        "- 処刑された相手が狂人でも、霊媒結果は『人狼ではありませんでした』になる。"
        "黒になるのは本物の人狼だけで、白結果だけでは村置き確定にはならない。\n"
        "- 処刑がまだ発生していない段階では断定を増やしすぎず、"
        "占い師 CO への反応を観察する。\n"
        "- 対抗霊媒が出た場合は、自分と相手どちらが真として整合するかを論理的に示す。\n"
        "- 占い師 CO を処刑して霊媒結果が白だった場合、それは占い師 CO 偽の証明ではない。"
        "真占い師だった可能性と、狂人など非狼の騙りだった可能性を分けて整理する。\n"
        "- 占い師 CO を偽視する場合は、霊媒白そのものではなく、"
        "対抗 CO、占い結果の破綻、発言時系列、投票、襲撃結果、死亡タイミングとの整合性を根拠にする。"
    ),
    Role.KNIGHT: (
        "- 守る価値の高い情報役 (真占い・真霊媒) や、信頼されている位置を護衛対象として意識する。\n"
        "- 同じ相手を連続で護衛してはならない。前夜と違う相手を選ぶ。\n"
        "- 自分の護衛先を不用意に公開しない。公開すると翌夜の噛み筋のヒントを"
        "人狼側に与えてしまう。\n"
        "- 騎士 CO は原則として追い詰められた局面に温存し、必要なら霊媒結果や"
        "盤面の整合を優先する。\n"
        "- 犠牲者が出ない平和な朝は、自分の護衛が成功した可能性が高い。"
        "このときは護衛先を添えて騎士 CO する価値が高く、"
        "守った相手を真寄り・白寄りに置く材料として村の推理を進められる。\n"
        "- 護衛成功を理由に CO するときは必ず護衛先を添える。"
        "護衛先を隠した騎士 CO は真偽判定されにくく信用されない。"
        "通常時 (平和ではない朝) の無意味な CO は引き続き避ける。"
    ),
    Role.VILLAGER: (
        "- 公開発言の矛盾、視点漏れ、投票理由、占い/霊媒結果との整合性を重視して推理する。\n"
        "- 不確実なときは候補を絞り、理由を添えて話す。曖昧な決めつけや"
        "『なんとなく怪しい』だけの発言は避ける。\n"
        "- 自分に私的情報があるふりをしない。占い/霊媒/騎士の CO 騙りは村陣営としては行わない。\n"
        "- 情報役を守り、人狼陣営が狙いやすい位置 (真 CO、盤面整理役) を"
        "投票で落とさないようにする。"
    ),
}


def _build_strategy_block(role: Role) -> str:
    """Return role-specific tips for the given role only.

    Caller must pass a non-None Role; `build_system_prompt` is invoked after
    SETUP so `player.role` is already assigned. Strictly role-scoped — never
    returns other roles' tips, so the system prompt cannot leak strategy
    between LLM seats.
    """
    return _ROLE_STRATEGIES[role]


def _build_speech_profile_block(persona: Persona) -> str:
    """Render the persona's structured speech profile as a bullet block.

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
        .replace("{speech_profile_block}", _build_speech_profile_block(persona))
        .replace("{role_block}", role_block)
        .replace("{strategy_block}", _build_strategy_block(role))
        .replace("{phase_block}", phase_block)
        .replace("{task_block}", task_text)
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

    return (
        f"あなたは座席 {my_seat.seat_no}『{my_seat.display_name}』です。\n"
        f"生存者: {alive_names}\n"
        f"死亡者: {dead_names}\n"
        f"現在フェイズ: {game.phase.value} / day {game.day_number}\n"
        f"{wolf_partner_block}"
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
def task_daytime_speech(day_number: int) -> str:
    return (
        f"現在は day {day_number} の議論フェイズです。"
        " 必要と感じた場合のみ `intent=speak` を返し、`public_message` に 80〜300 字で短い発言を書いてください。"
        " 発言したくない場合は `intent=skip` と明示してください。"
    )


def task_vote(candidate_tokens: Sequence[str], runoff: bool) -> str:
    """Candidates are `席{N} {display_name}` tokens; target_name must echo one back."""
    names = "、".join(candidate_tokens)
    runoff_note = "これは決選投票です。" if runoff else ""
    return (
        f"{runoff_note}投票先として合法な候補は: {names}\n"
        " `intent=vote`、`target_name` に候補トークン (例: `席3 Alice`) のいずれかを"
        " 厳密に一致させて返してください。`席番号` を含めないと同名の別席と区別できません。"
        " どうしても棄権したい場合は `intent=skip` を返し、`target_name` は `null` にします。"
    )


def task_night_action(kind: SubmissionType, candidate_tokens: Sequence[str]) -> str:
    """Candidates are `席{N} {display_name}` tokens; target_name must echo one back."""
    names = "、".join(candidate_tokens)
    label = {
        SubmissionType.WOLF_ATTACK: "襲撃",
        SubmissionType.SEER_DIVINE: "占い",
        SubmissionType.KNIGHT_GUARD: "護衛",
    }[kind]
    extra = ""
    if kind is SubmissionType.WOLF_ATTACK:
        extra = (
            " 仲間の人狼が人狼チャットで案を出している場合、強い反対理由がなければ"
            " その案に合わせてください。意見が割れると襲撃が空振りになります。"
        )
    return (
        f"夜です。{label} 対象を 1 名選んでください。合法候補: {names}\n"
        " `intent=night_action`、`target_name` に候補トークン (例: `席3 Alice`) のいずれかを"
        " 厳密に一致させて返してください。`席番号` を含めないと同名の別席と区別できません。"
        f"{extra}"
    )


def task_wolf_chat(partner_tokens: Sequence[str], candidate_tokens: Sequence[str]) -> str:
    """Ask a wolf to post a short coordination message to the wolves-only chat."""
    partners = "、".join(partner_tokens) if partner_tokens else "(なし)"
    names = "、".join(candidate_tokens)
    return (
        f"夜になりました。仲間の人狼: {partners}。人狼チャット (村人には非公開) で"
        f" 襲撃対象を調整してください。候補: {names}\n"
        " `intent=speak` と `public_message` に 1 名の襲撃候補とその理由を"
        " 80〜150 字で書いてください。仲間が既に案を出している場合は、賛成か反対か"
        " を明示し、最終的に 1 人に揃えることを優先してください。話すことがなければ"
        " `intent=skip` を返してください。"
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
