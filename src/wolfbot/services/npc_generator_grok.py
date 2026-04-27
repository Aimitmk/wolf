"""Concrete NpcGenerator that calls xAI Grok for reactive speech.

Given a ``LogicPacket`` (summarised game state, logic candidates, pressure
map) and a ``SpeakRequest`` (max chars, phase, intent), this module builds
a minimal Japanese prompt and hits the xAI chat completions endpoint with
structured JSON output.

The prompt is deliberately simpler than the full ``llm_service`` prompt
pipeline — reactive utterances are short (80-char cap) situational remarks,
not multi-paragraph analytical speeches. The persona's ``style_guide`` and
``speech_profile`` are included for voice consistency but the strategic
rules sections are omitted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from wolfbot.domain.ws_messages import LogicCandidate, LogicPacket, SpeakRequest
from wolfbot.llm.personas import PERSONAS, Persona
from wolfbot.services.npc_speech_service import NpcGeneratedSpeech

log = logging.getLogger(__name__)

PERSONAS_BY_KEY: dict[str, Persona] = {p.key: p for p in PERSONAS}

_RESPONSE_SCHEMA: dict[str, object] = {
    "name": "reactive_speech",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["text", "intent", "used_logic_ids"],
        "properties": {
            "text": {"type": "string", "maxLength": 300},
            "intent": {
                "type": "string",
                "enum": ["speak", "agree", "disagree", "question", "accuse", "defend", "skip"],
            },
            "used_logic_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    },
}


def _build_system(persona: Persona, max_chars: int) -> str:
    sp = persona.speech_profile
    speech_block = (
        f"一人称: {sp.first_person}\n"
        f"語尾/文体: {sp.sentence_style}\n"
        f"間の取り方: {sp.pause_style}\n"
    )
    if sp.signature_phrases:
        speech_block += f"特徴語(低頻度): {'、'.join(sp.signature_phrases)}\n"
    return (
        "あなたは人狼ゲームに参加中のプレイヤーです。\n"
        f"キャラクター名: {persona.display_name}\n"
        f"性格: {persona.style_guide}\n"
        f"## 話法\n{speech_block}\n"
        "## ルール\n"
        "- 日本語のみ。メタ発言禁止。AIであることに言及しない。\n"
        f"- `text` は {max_chars} 文字以内の短い発言。\n"
        "- 発言しない場合は intent を `skip`、text を空文字にする。\n"
        "- `used_logic_ids` には参考にした logic candidate の id を入れる。\n"
    )


def _build_user(logic: LogicPacket, request: SpeakRequest) -> str:
    lines = [
        f"フェイズ: {request.phase_id}",
        f"提案意図: {request.suggested_intent}",
        "",
        "## 場の状況",
        logic.public_state_summary or "(情報なし)",
    ]
    if logic.logic_candidates:
        lines.append("")
        lines.append("## 論点候補")
        for c in logic.logic_candidates:
            lines.append(_format_candidate(c))
    if logic.pressure:
        lines.append("")
        lines.append("## 圧力マップ (席番号 → 疑い度)")
        for seat, val in sorted(logic.pressure.items()):
            lines.append(f"  席{seat}: {val:.2f}")
    lines.append("")
    lines.append("上記を踏まえ、キャラクターとして自然な短い発言を生成してください。")
    return "\n".join(lines)


def _format_candidate(c: LogicCandidate) -> str:
    parts = [f"- [{c.id}] {c.claim}"]
    if c.support:
        parts.append(f"  根拠: {'、'.join(c.support)}")
    if c.counter:
        parts.append(f"  反論: {'、'.join(c.counter)}")
    return "\n".join(parts)


@dataclass
class GrokNpcGeneratorConfig:
    model: str = "grok-4-1-fast"
    timeout: float = 15.0
    temperature: float = 0.8
    default_persona_key: str = "setsu"


class GrokNpcGenerator:
    """Production NpcGenerator backed by xAI Grok."""

    def __init__(
        self,
        *,
        api_key: str,
        config: GrokNpcGeneratorConfig | None = None,
    ) -> None:
        self._api_key = api_key
        self.config = config or GrokNpcGeneratorConfig()
        self._persona_key: str | None = None

    def set_persona(self, persona_key: str) -> None:
        """Set the persona key for this NPC. Called after seat assignment."""
        self._persona_key = persona_key

    async def generate(
        self,
        *,
        logic: LogicPacket,
        request: SpeakRequest,
    ) -> NpcGeneratedSpeech | None:
        from openai import AsyncOpenAI

        persona = PERSONAS_BY_KEY.get(
            self._persona_key or self.config.default_persona_key, PERSONAS[0]
        )
        system = _build_system(persona, max_chars=request.max_chars)
        user = _build_user(logic, request)

        client = AsyncOpenAI(
            api_key=self._api_key,
            base_url="https://api.x.ai/v1",
        )
        try:
            resp = await client.chat.completions.create(  # type: ignore[call-overload]
                model=self.config.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": _RESPONSE_SCHEMA,
                },
                temperature=self.config.temperature,
                timeout=self.config.timeout,
            )
        except Exception:
            log.exception("grok_npc_generate_failed")
            return None

        content = resp.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            log.warning("grok_npc_invalid_json response=%s", content[:200])
            return None

        text = data.get("text", "").strip()
        intent = data.get("intent", "speak")
        if intent == "skip" or not text:
            return None

        used_ids = tuple(data.get("used_logic_ids", []))
        # Rough estimate: ~150ms per character for TTS
        estimated_ms = max(500, len(text) * 150)

        return NpcGeneratedSpeech(
            text=text,
            intent=intent,
            used_logic_ids=used_ids,
            estimated_duration_ms=estimated_ms,
        )


__all__ = ["GrokNpcGenerator", "GrokNpcGeneratorConfig"]
