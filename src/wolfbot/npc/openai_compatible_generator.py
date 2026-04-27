"""Concrete NpcGenerator that calls any OpenAI-compatible chat-completions
endpoint for reactive speech.

Given a ``LogicPacket`` (summarised game state, logic candidates, pressure
map) and a ``SpeakRequest`` (max chars, phase, intent), this module builds
a minimal Japanese prompt and hits the configured chat-completions endpoint
with structured JSON output.

The provider is intentionally not baked into the class name. Swap it by
changing :class:`OpenAICompatibleConfig.base_url` and ``model``:

* xAI Grok — ``base_url="https://api.x.ai/v1"``, ``model="grok-..."``
* OpenAI — ``base_url="https://api.openai.com/v1"``, ``model="gpt-..."``
* Groq — ``base_url="https://api.groq.com/openai/v1"``
* Together AI — ``base_url="https://api.together.xyz/v1"``
* vLLM / Ollama (OpenAI-compatible mode) — local ``base_url``

The default is xAI for back-compat with existing deployments. The prompt is
deliberately simpler than the full ``llm_service`` prompt pipeline —
reactive utterances are short (80-char cap) situational remarks, not
multi-paragraph analytical speeches. The persona's ``style_guide`` and
``speech_profile`` are included for voice consistency but the strategic
rules sections are omitted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from wolfbot.domain.ws_messages import LogicCandidate, LogicPacket, SpeakRequest
from wolfbot.llm.persona_base import Persona
from wolfbot.npc.personas import NPC_PERSONAS_BY_KEY
from wolfbot.npc.speech_service import NpcGeneratedSpeech

log = logging.getLogger(__name__)

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
class OpenAICompatibleConfig:
    """Backend-agnostic config for any OpenAI Chat Completions endpoint.

    Defaults target xAI Grok for back-compat; override ``base_url`` and
    ``model`` to point at OpenAI, Groq, Together, vLLM, Ollama, etc.
    """

    model: str = "grok-4-1-fast"
    base_url: str = "https://api.x.ai/v1"
    timeout: float = 15.0
    temperature: float = 0.8


class OpenAICompatibleNpcGenerator:
    """Production NpcGenerator backed by any OpenAI-compatible LLM endpoint.

    Implements :class:`wolfbot.npc.speech_service.NpcGenerator` via the
    ``openai`` SDK's ``chat.completions`` API. The choice of provider is
    a config decision (``base_url`` + ``model``), not a code decision.
    """

    def __init__(
        self,
        *,
        api_key: str,
        config: OpenAICompatibleConfig | None = None,
    ) -> None:
        self._api_key = api_key
        self.config = config or OpenAICompatibleConfig()
        self._persona_key: str | None = None

    def set_persona(self, persona_key: str) -> None:
        """Set the persona key for this NPC. Must be called once at startup,
        before any ``generate()`` invocation.  Raises if the key is unknown.
        """
        if persona_key not in NPC_PERSONAS_BY_KEY:
            valid = ", ".join(sorted(NPC_PERSONAS_BY_KEY.keys()))
            raise ValueError(
                f"unknown persona_key {persona_key!r}; valid keys: {valid}"
            )
        self._persona_key = persona_key

    async def generate(
        self,
        *,
        logic: LogicPacket,
        request: SpeakRequest,
    ) -> NpcGeneratedSpeech | None:
        from openai import AsyncOpenAI

        if self._persona_key is None:
            raise RuntimeError(
                "OpenAICompatibleNpcGenerator.generate() called before set_persona(); "
                "each NPC bot must declare its persona at startup."
            )
        persona = NPC_PERSONAS_BY_KEY[self._persona_key]
        system = _build_system(persona, max_chars=request.max_chars)
        user = _build_user(logic, request)

        client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self.config.base_url,
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
            log.exception(
                "npc_generate_failed model=%s base_url=%s",
                self.config.model, self.config.base_url,
            )
            return None

        content = resp.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            log.warning("npc_generate_invalid_json response=%s", content[:200])
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


__all__ = ["OpenAICompatibleConfig", "OpenAICompatibleNpcGenerator"]
