"""Construct system + user messages for xAI calls.

Public functions build plain-string prompts so the xAI layer can stay transport-agnostic.
Inputs are domain models; outputs are strings. No I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from wolfbot.domain.enums import (
    FACTION_JA,
    ROLE_JA,
    Phase,
    Role,
    SubmissionType,
)
from wolfbot.domain.models import Game, Player, Seat
from wolfbot.llm.personas import Persona

SYSTEM_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "prompts" / "llm_system_prompt.md"


def _load_template() -> str:
    return SYSTEM_TEMPLATE_PATH.read_text(encoding="utf-8")


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
        template.replace("{persona_block}", persona_block)
        .replace("{role_block}", role_block)
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
