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
from wolfbot.llm.template import render_template
from wolfbot.npc.decision.game_state import NpcGameState

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
    """Render the bot's private state mirror as a Japanese prompt block.

    Naming policy: 席番号は冒頭の `## 参加者` ロスター 1 ブロックだけに
    集約し、ここから下は display_name のみで参照する。data 層 (vote
    target_seat 等) には数字を入れる。
    """
    own_name: str | None = None
    for s, n in state.alive_seats:
        if s == state.seat_no:
            own_name = n
            break
    own_label = (
        f"{own_name} (席{state.seat_no})" if own_name else f"席{state.seat_no}"
    )
    lines: list[str] = [
        f"あなた: {own_label}",
        f"あなたの役職: {state.role}",
    ]
    causes = state.dead_seat_causes

    def _cause(seat_no: int) -> str:
        c = causes.get(seat_no)
        return " (処刑)" if c == "EXECUTION" else " (襲撃)" if c == "ATTACK" else ""

    if state.alive_seats or state.dead_seats:
        lines.append("")
        lines.append("## 参加者 (席番号 → 名前)")
        if state.alive_seats:
            lines.append("生存中:")
            for s, n in state.alive_seats:
                lines.append(f"  席{s} {n}")
        if state.dead_seats:
            lines.append("死亡:")
            for s, n in state.dead_seats:
                lines.append(f"  席{s} {n}{_cause(s)}")
    if state.partner_wolves:
        partners = "、".join(n for _s, n in state.partner_wolves)
        lines.append(f"仲間の人狼 (非公開): {partners}")
    if state.seer_results:
        lines.append("## 自分の占い結果 (非公開)")
        for sr in state.seer_results:
            verdict = "黒 (人狼)" if sr.is_wolf else "白 (人狼ではない)"
            lines.append(f"  day{sr.day}: {sr.target_name} → {verdict}")
    if state.medium_results:
        lines.append("## 自分の霊媒結果 (非公開)")
        for mr in state.medium_results:
            if mr.is_wolf is None:
                verdict = "結果なし (処刑なし)"
            elif mr.is_wolf:
                verdict = "人狼"
            else:
                verdict = "人狼ではない"
            lines.append(f"  day{mr.day}: {mr.target_name} → {verdict}")
    if state.guard_history:
        lines.append("## 自分の護衛履歴 (非公開)")
        for g in state.guard_history:
            outcome = (
                "(平和な朝)" if g.peaceful_morning
                else "(襲撃発生)" if g.peaceful_morning is False
                else "(結果未確定)"
            )
            lines.append(
                f"  day{g.day}: {g.target_name} を護衛 {outcome}"
            )
    if state.wolf_chat_history:
        lines.append("## 人狼チャット履歴 (狼/狂人にのみ見える)")
        for line in state.wolf_chat_history[-20:]:
            lines.append(
                f"  day{line.day} {line.speaker_name}: {line.text}"
            )
    if state.wolf_attack_history:
        lines.append("## 自分達の襲撃履歴 (非公開)")
        for atk in state.wolf_attack_history:
            if atk.peaceful_morning is True:
                outcome = "(平和な朝 = GJ)"
            elif atk.peaceful_morning is False:
                outcome = "(襲撃成功)"
            else:
                outcome = "(結果未確定)"
            lines.append(
                f"  day{atk.day}: {atk.target_name} を襲撃 {outcome}"
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
    """Compose the system + user prompt for a vote decision.

    Templates:
    - npc/decision_vote_system.md  (fixed instruction)
    - npc/decision_vote_user.md    (per-call context)
    """
    return (
        render_template(_DECISION_VOTE_SYSTEM_TEMPLATE),
        render_template(
            _DECISION_VOTE_USER_TEMPLATE,
            round_label=_VOTE_ACT_TEXT_BY_ROUND.get(
                request.round_, f"round={request.round_}"
            ),
            day_number=state.day_number,
            persona_block=_build_persona_block(persona),
            role_block=_build_role_block(state.role),
            state_block=_build_state_block(state),
            digest=request.public_state_summary or "(情報なし)",
            candidates_str=_format_candidates(request.candidate_seats),
        ),
    )


_DECISION_VOTE_SYSTEM_TEMPLATE = "npc/decision_vote_system"
_DECISION_VOTE_USER_TEMPLATE = "npc/decision_vote_user"


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


_DECISION_WOLF_CHAT_SYSTEM_TEMPLATE = "npc/decision_wolf_chat_system"
_DECISION_WOLF_CHAT_USER_TEMPLATE = "npc/decision_wolf_chat_user"


def build_wolf_chat_prompt(
    *,
    state: NpcGameState,
    persona: Persona,
    candidates: Sequence[tuple[int, str]],
    public_state_summary: str,
) -> tuple[str, str]:
    """Compose system + user prompts for a wolf-chat coordination line.

    Templates:
    - npc/decision_wolf_chat_system.md (fixed: voice, JSON shape, GJ rebite rule)
    - npc/decision_wolf_chat_user.md   (per-call context)

    Wolves talk to each other privately. The line must propose / agree
    / counter on a target, stay under 80 chars, and speak in the
    persona's voice (this is still character).

    The role-strategy block (= ``build_strategy_block(WEREWOLF)``) is
    INJECTED via :func:`_build_role_block`. Without it, the chat's
    "今夜誰を噛む?" decision runs on the persona block + game ledger
    only; the master tactical rules (multi-CO attack avoidance, GJ
    rebite, info-role priority, knight-candidate scoring) live in the
    WEREWOLF strategy block and were missing from the chat prompt —
    so wolves agreed to attack a seer in a 3-CO board (game
    ``38627df1ade1`` night 1), and the night-action prompt that
    follows already carries the chat history as commitment, biasing
    both wolves to follow through.
    """
    candidates_str = (
        "、".join(f"席{seat_no} {name}" for seat_no, name in candidates)
        or "(なし)"
    )
    return (
        render_template(_DECISION_WOLF_CHAT_SYSTEM_TEMPLATE),
        render_template(
            _DECISION_WOLF_CHAT_USER_TEMPLATE,
            day_number=state.day_number,
            persona_block=_build_persona_block(persona),
            role_block=_build_role_block(state.role),
            state_block=_build_state_block(state),
            digest=public_state_summary or "(情報なし)",
            candidates_str=candidates_str,
        ),
    )


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


_DECISION_NIGHT_SYSTEM_TEMPLATE = "npc/decision_night_system"
_DECISION_NIGHT_USER_TEMPLATE = "npc/decision_night_user"


def build_night_prompt(
    *,
    state: NpcGameState,
    persona: Persona,
    request: DecideNightActionRequest,
) -> tuple[str, str]:
    """Compose the system + user prompt for a night-action decision.

    Templates:
    - npc/decision_night_system.md (fixed instruction)
    - npc/decision_night_user.md   (per-call context)
    """
    return (
        render_template(_DECISION_NIGHT_SYSTEM_TEMPLATE),
        render_template(
            _DECISION_NIGHT_USER_TEMPLATE,
            action_label=_NIGHT_ACT_TEXT.get(
                request.action_kind, request.action_kind
            ),
            day_number=state.day_number,
            persona_block=_build_persona_block(persona),
            role_block=_build_role_block(state.role),
            state_block=_build_state_block(state),
            digest=request.public_state_summary or "(情報なし)",
            candidates_str=_format_candidates(request.candidate_seats),
        ),
    )


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
