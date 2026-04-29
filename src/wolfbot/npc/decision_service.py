"""NPC bot per-seat decision LLM — votes / night actions / runoff votes.

Phase-D: each NPC bot owns the strategic decisions for its seat. This
service is the LLM-backed decision layer; it consumes the bot's
`NpcGameState` mirror (role + role-specific results + wolf chat) plus
the request-time public-state digest, calls `NPC_LLM_*` with a strict
JSON schema, and returns the chosen target seat.

Returns ``target_seat=None`` on:
* missing or stale game state (snapshot never received),
* model error / timeout (caller already enforces a wall-clock deadline),
* schema validation failure (model returned a non-candidate or a
  malformed JSON payload).

A None result is logged and propagated up to the WS reply — Master
records it as an abstain/skip per the user's "log it so the viewer
shows the seat went silent" rule.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from wolfbot.domain.enums import Role
from wolfbot.domain.ws_messages import (
    DecideNightActionRequest,
    DecideVoteRequest,
)
from wolfbot.llm.persona_base import Persona
from wolfbot.llm.prompt_builder import (
    build_judgment_profile_block,
    build_speech_profile_block,
    build_strategy_block,
)
from wolfbot.npc.game_state import NpcGameState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecisionResult:
    """Decision LLM output. ``target_seat=None`` means abstain / skip."""

    target_seat: int | None
    reason_summary: str = ""


@runtime_checkable
class DecisionLLM(Protocol):
    """Provider-agnostic decision call. Returns raw JSON text. Implementations
    set the JSON schema appropriately (xAI strict json_schema, DeepSeek
    json_object, Gemini response_json_schema)."""

    async def decide_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, object],
    ) -> str: ...


# ---------------------------------------------------------------- prompt blocks


_VOTE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["target_seat", "reason"],
    "properties": {
        "target_seat": {
            "type": "integer",
            "description": (
                "Seat number of the vote target. Must be one of the "
                "supplied candidate seats. Abstaining (null) is forbidden — "
                "every alive voter has to pick someone."
            ),
        },
        "reason": {
            "type": "string",
            "description": "Short internal note explaining the choice.",
        },
    },
}


_NIGHT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["target_seat", "reason"],
    "properties": {
        "target_seat": {
            "type": "integer",
            "description": (
                "Seat number of the night-action target. Must be one of "
                "the supplied candidate seats. Skipping (null) is forbidden — "
                "every wolf_attack / seer_divine / knight_guard must pick "
                "an actual target. Master rejects null with ILLEGAL_TARGET "
                "and the night phase stalls on missing submissions, so "
                "the decision LLM has to commit to a candidate."
            ),
        },
        "reason": {
            "type": "string",
            "description": "Short internal note explaining the choice.",
        },
    },
}


def _build_state_block(state: NpcGameState) -> str:
    """Render the bot's private state mirror as a Japanese prompt block."""
    lines = [f"あなたの席: 席{state.seat_no}", f"あなたの役職: {state.role}"]
    if state.alive_seats:
        alive = "、".join(f"席{s} {n}" for s, n in state.alive_seats)
        lines.append(f"生存者: {alive}")
    if state.dead_seats:
        causes = state.dead_seat_causes
        def _cause(seat_no: int) -> str:
            c = causes.get(seat_no)
            return " (処刑)" if c == "EXECUTION" else " (襲撃)" if c == "ATTACK" else ""
        dead = "、".join(
            f"席{s} {n}{_cause(s)}" for s, n in state.dead_seats
        )
        lines.append(f"死亡者: {dead}")
    if state.partner_wolves:
        partners = "、".join(f"席{s} {n}" for s, n in state.partner_wolves)
        lines.append(f"仲間の人狼 (非公開): {partners}")
    if state.seer_results:
        lines.append("## 自分の占い結果 (非公開)")
        for sr in state.seer_results:
            verdict = "黒 (人狼)" if sr.is_wolf else "白 (人狼ではない)"
            lines.append(f"  day{sr.day}: 席{sr.target_seat} {sr.target_name} → {verdict}")
    if state.medium_results:
        lines.append("## 自分の霊媒結果 (非公開)")
        for mr in state.medium_results:
            if mr.is_wolf is None:
                verdict = "結果なし (処刑なし)"
            elif mr.is_wolf:
                verdict = "人狼"
            else:
                verdict = "人狼ではない"
            lines.append(f"  day{mr.day}: 席{mr.target_seat} {mr.target_name} → {verdict}")
    if state.guard_history:
        lines.append("## 自分の護衛履歴 (非公開)")
        for g in state.guard_history:
            outcome = (
                "(平和な朝)" if g.peaceful_morning
                else "(襲撃発生)" if g.peaceful_morning is False
                else "(結果未確定)"
            )
            lines.append(
                f"  day{g.day}: 席{g.target_seat} {g.target_name} を護衛 {outcome}"
            )
    if state.wolf_chat_history:
        lines.append("## 人狼チャット履歴 (狼/狂人にのみ見える)")
        for line in state.wolf_chat_history[-20:]:
            lines.append(
                f"  day{line.day} 席{line.speaker_seat} {line.speaker_name}: {line.text}"
            )
    return "\n".join(lines)


def _build_persona_block(persona: Persona) -> str:
    return (
        f"## キャラクター\n"
        f"名前: {persona.display_name}\n"
        f"性格指針: {persona.style_guide}\n\n"
        f"## 話法\n{build_speech_profile_block(persona)}\n\n"
        f"## 判断のクセ\n{build_judgment_profile_block(persona)}"
    )


def _build_role_block(role_str: str) -> str:
    """Best-effort role-strategy block. Falls back to empty when the role
    string isn't a known `Role` enum value (defensive — the snapshot
    always carries a canonical name, but a future server version could
    add roles)."""
    try:
        role = Role(role_str)
    except ValueError:
        return ""
    return f"## 役職別の戦術ヒント\n{build_strategy_block(role)}"


def _format_candidates(candidates: Iterable[tuple[int, str]]) -> str:
    return "、".join(f"席{seat_no} {name}" for seat_no, name in candidates) or "(なし)"


# ---------------------------------------------------------------- decision API


_VOTE_ACT_TEXT_BY_ROUND: dict[int, str] = {
    0: "通常投票",
    1: "決選投票",
}


def build_vote_prompt(
    *,
    state: NpcGameState,
    persona: Persona,
    request: DecideVoteRequest,
) -> tuple[str, str]:
    """Compose the system + user prompt for a vote decision."""
    candidates_str = _format_candidates(request.candidate_seats)
    digest = request.public_state_summary or "(情報なし)"
    round_label = _VOTE_ACT_TEXT_BY_ROUND.get(request.round_, f"round={request.round_}")
    state_block = _build_state_block(state)
    persona_block = _build_persona_block(persona)
    role_block = _build_role_block(state.role)
    system = (
        "あなたは人狼ゲームの 1 プレイヤーです。"
        "公開情報・自分が持つ非公開情報・性格・役職戦術に基づいて投票先を決めてください。"
        "返答は JSON のみ。"
    )
    user_parts = [
        f"## フェイズ: {round_label} (day {state.day_number})",
        "",
        persona_block,
        "",
        role_block,
        "",
        "## 自分の状況 (非公開を含む)",
        state_block,
        "",
        "## 場の状況 (Master ダイジェスト)",
        digest,
        "",
        f"## 投票候補席\n{candidates_str}",
        "",
        "上記すべてを踏まえ、この投票で誰に票を入れるかを決めてください。"
        "**棄権は禁止**: 必ず候補席の中から1人を選んで `target_seat` に入れる。"
        "情報が薄くても、最も怪しい/役割上吊りたい/相方ライン以外の中から相対的に最も票を入れたい1人を選ぶこと。"
        "JSON は {\"target_seat\": <候補席番号>, \"reason\": \"<短い理由>\"} の形 "
        "(`target_seat` は必ず整数、null 不可)。",
    ]
    return system, "\n".join(p for p in user_parts if p is not None)


_NIGHT_ACT_TEXT: dict[str, str] = {
    "wolf_attack": "人狼の襲撃",
    "seer_divine": "占い師の占い",
    "knight_guard": "騎士の護衛",
}


_WOLF_CHAT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {
        "text": {
            "type": "string",
            "description": "短い coordinator メッセージ (狼仲間にだけ届く)。最大 80 文字。",
        },
    },
}


def build_wolf_chat_prompt(
    *,
    state: NpcGameState,
    persona: Persona,
    candidates: Sequence[tuple[int, str]],
    public_state_summary: str,
) -> tuple[str, str]:
    """Compose system + user prompts for a wolf-chat coordination line.

    Wolves talk to each other privately. The line must:
    - propose / agree / counter on a target,
    - stay under 80 chars,
    - speak in the persona's voice (this is still character).
    """
    persona_block = _build_persona_block(persona)
    state_block = _build_state_block(state)
    candidates_str = (
        "、".join(f"席{seat_no} {name}" for seat_no, name in candidates)
        or "(なし)"
    )
    digest = public_state_summary or "(情報なし)"
    system = (
        "あなたは人狼ゲームの 1 プレイヤーです。"
        "あなたは人狼で、仲間の人狼にだけ届く秘密チャットでこのターンの "
        "襲撃方針を簡潔に伝えてください。村人に届く発話ではないので、"
        "ペルソナの口調を保ちつつ素直に作戦を提示してよい (ただし"
        "メタ用語は避ける)。返答は JSON のみ。"
    )
    user_parts = [
        f"## 現在: 人狼チャット (day {state.day_number})",
        "",
        persona_block,
        "",
        "## 自分の状況 (非公開)",
        state_block,
        "",
        "## 場の状況 (Master ダイジェスト)",
        digest,
        "",
        f"## 襲撃候補席\n{candidates_str}",
        "",
        "上記を踏まえ、仲間の狼に向けて 80 文字以内で 1 行だけ書いてください。"
        "JSON は {\"text\": \"...\"} の形。",
    ]
    return system, "\n".join(p for p in user_parts if p is not None)


def parse_wolf_chat_text(raw_json: str) -> str | None:
    """Pull the ``text`` field out of the JSON response, with empty / non-string
    payloads dropped to None so the dispatcher records a no-line outcome."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("text")
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    return cleaned or None


def build_night_prompt(
    *,
    state: NpcGameState,
    persona: Persona,
    request: DecideNightActionRequest,
) -> tuple[str, str]:
    """Compose the system + user prompt for a night-action decision."""
    candidates_str = _format_candidates(request.candidate_seats)
    digest = request.public_state_summary or "(情報なし)"
    state_block = _build_state_block(state)
    persona_block = _build_persona_block(persona)
    role_block = _build_role_block(state.role)
    action_label = _NIGHT_ACT_TEXT.get(request.action_kind, request.action_kind)
    system = (
        "あなたは人狼ゲームの 1 プレイヤーです。"
        "夜行動の対象を性格・役職戦術・公開/非公開情報を踏まえて決めてください。"
        "返答は JSON のみ。"
    )
    user_parts = [
        f"## フェイズ: {action_label} (day {state.day_number})",
        "",
        persona_block,
        "",
        role_block,
        "",
        "## 自分の状況 (非公開を含む)",
        state_block,
        "",
        "## 場の状況 (Master ダイジェスト)",
        digest,
        "",
        f"## 行動候補席\n{candidates_str}",
        "",
        "上記すべてを踏まえ、夜の行動対象を決めてください。"
        "**スキップ禁止**: 必ず候補席の中から1人を選んで `target_seat` に入れる。"
        "情報が薄くても、相対的に最も対象として価値がある1人を選ぶこと "
        "(占い: 情報を取りたい灰、人狼: 噛み価値の高い位置、騎士: 守るべき情報役/重要位置)。"
        "「捨て護衛」のような戦術選択をしたい場合も、null ではなく合法候補から1人を選ぶ。"
        "JSON は {\"target_seat\": <候補席番号>, \"reason\": \"<短い理由>\"} の形 "
        "(`target_seat` は必ず整数、null 不可)。",
    ]
    return system, "\n".join(p for p in user_parts if p is not None)


def parse_decision(
    raw_json: str,
    *,
    legal_seats: frozenset[int],
) -> DecisionResult:
    """Parse + validate a decision LLM response.

    Returns ``DecisionResult(target_seat=None)`` on any failure: malformed
    JSON, missing fields, target outside ``legal_seats``, etc. The
    abstain default keeps the bot game-legal even when the model
    misbehaves.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        log.warning("decision_parse_json_failed raw=%s", raw_json[:200])
        return DecisionResult(target_seat=None, reason_summary="parse_failed")
    if not isinstance(data, dict):
        return DecisionResult(target_seat=None, reason_summary="not_object")
    raw_target = data.get("target_seat")
    if raw_target is None:
        return DecisionResult(target_seat=None, reason_summary=str(data.get("reason", "")))
    if not isinstance(raw_target, int) or isinstance(raw_target, bool):
        return DecisionResult(target_seat=None, reason_summary="non_int_target")
    if raw_target not in legal_seats:
        log.info("decision_target_out_of_set target=%s legal=%s", raw_target, legal_seats)
        return DecisionResult(target_seat=None, reason_summary="illegal_target")
    return DecisionResult(target_seat=raw_target, reason_summary=str(data.get("reason", "")))


__all__ = [
    "_NIGHT_SCHEMA",
    "_VOTE_SCHEMA",
    "_WOLF_CHAT_SCHEMA",
    "DecisionLLM",
    "DecisionResult",
    "build_night_prompt",
    "build_vote_prompt",
    "build_wolf_chat_prompt",
    "parse_decision",
    "parse_wolf_chat_text",
]
