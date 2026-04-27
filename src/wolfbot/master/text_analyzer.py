"""Text-channel utterance analyzer — mirrors `GeminiAudioAnalyzer`.

Voice utterances go through `GeminiAudioAnalyzer` (audio → transcript +
structured fields). Text-channel utterances were previously persisted raw
with no structure, so a human typing a direct address ("〜さん、どう思う"
style) never set `SpeechEvent.addressed_seat_no` and the SpeakArbiter
could not route the reply.

This module gives the text path the same structured signal:

  TextAnalyzer.analyze(text) → TextAnalysis(
      addressed_name: str | None,    # 'セツ' / '席3' / None
      co_declaration: str | None,    # 'seer' / 'medium' / 'knight' / None
  )

Master then resolves `addressed_name` via the same `resolve_seat_by_name`
helper used by the voice path and passes both fields to
`make_human_text_event(...)`. The downstream pipeline (DiscussionService,
PublicDiscussionState fold, SpeakArbiter picker, LogicPacket summary) is
identical for text and voice — only the analyzer differs.

Providers:
- ``GeminiTextAnalyzer`` — production. Sends one cheap Gemini call per
  message (no audio, ~25-token prompt + the message text).
- ``FakeTextAnalyzer`` — deterministic stub for tests.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TextAnalysis:
    """Structured analysis of a single text-channel utterance."""

    addressed_name: str | None = None
    co_declaration: str | None = None


class TextAnalyzerError(RuntimeError):
    """Raised on hard provider failures (timeout, 5xx, malformed response)."""

    def __init__(self, failure_reason: str) -> None:
        super().__init__(failure_reason)
        self.failure_reason = failure_reason


@runtime_checkable
class TextAnalyzer(Protocol):
    """Async analyzer used by `WolfCog.on_message`. Must be cancellable and
    must not block the asyncio loop on the network call."""

    async def analyze(self, *, text: str, timeout_s: float) -> TextAnalysis: ...


class FakeTextAnalyzer:
    """In-memory analyzer for tests.

    Either return a scripted sequence of results / errors, or a fixed
    `default`. Tests can also pass a callable to derive the analysis from
    the input text.
    """

    def __init__(
        self,
        scripted: list[TextAnalysis | Exception] | None = None,
        default: TextAnalysis | None = None,
    ) -> None:
        self._scripted: list[TextAnalysis | Exception] = list(scripted or [])
        self._default: TextAnalysis | None = default
        self.call_count = 0
        self.last_text: str | None = None

    async def analyze(self, *, text: str, timeout_s: float) -> TextAnalysis:
        self.call_count += 1
        self.last_text = text
        if self._scripted:
            head = self._scripted.pop(0)
            if isinstance(head, Exception):
                raise head
            return head
        if self._default is None:
            return TextAnalysis()
        return self._default


class GeminiTextAnalyzer:
    """Gemini text-only analyzer. Schema mirrors `GeminiAudioAnalyzer`.

    Uses ``httpx`` lazily (no module-level import) so test environments
    without httpx still load this file. Default model is the same cheap
    flash-lite used for audio so cost stays bounded.
    """

    _SYSTEM_PROMPT: str = (
        "あなたは人狼ゲームのチャット発言分析エンジンです。\n"
        "渡された発言テキスト(日本語)を読み、以下のJSON形式で返してください。\n"
        "JSONのみ返答し、他のテキストは含めないでください。\n\n"
        "```json\n"
        "{\n"
        '  "co_claim": null,\n'
        '  "addressed_name": null\n'
        "}\n"
        "```\n\n"
        "フィールド説明:\n"
        "- co_claim: 役職CO(自称)があれば \"seer\"/\"medium\"/\"knight\"、なければ null\n"
        "- addressed_name: 特定のプレイヤーへの呼びかけがあればその名前(例 \"セツ\"、\"ジーナさん\"、\"席3\"、\"3番\")、なければ null。"
        "「みんな」「全員」など全体への呼びかけは null。さん/くん/ちゃん 等の敬称は付けたままでも構わない。"
        "発言内で他プレイヤーに言及するだけ(例: 『セツの判定が気になる』)は呼びかけではないので null。"
        "明確な宛先のある呼びかけ(例: 『セツさん、どう思う』)のみ設定。"
    )

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.0-flash-lite",
        api_base: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_s: float = 8.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.timeout_s = timeout_s

    async def analyze(self, *, text: str, timeout_s: float) -> TextAnalysis:
        import httpx

        effective_timeout = min(timeout_s, self.timeout_s)
        body = {
            "contents": [
                {
                    "parts": [
                        {"text": self._SYSTEM_PROMPT},
                        {"text": f"発言:\n{text}"},
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.0,
            },
        }
        url = (
            f"{self.api_base}/models/{self.model}:generateContent"
            f"?key={self.api_key}"
        )
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                resp = await client.post(url, json=body)
                if resp.status_code != 200:
                    raise TextAnalyzerError(f"gemini_http_{resp.status_code}")
                resp_json = resp.json()
                raw_text = (
                    resp_json.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
                parsed = self._parse_response(raw_text)
        except TextAnalyzerError:
            raise
        except httpx.TimeoutException as exc:
            raise TextAnalyzerError("gemini_timeout") from exc
        except httpx.ConnectError as exc:
            raise TextAnalyzerError("gemini_connection_refused") from exc
        except Exception as exc:
            raise TextAnalyzerError(
                f"gemini_unexpected_{type(exc).__name__}"
            ) from exc

        co_raw = parsed.get("co_claim")
        co_declaration = (
            co_raw if co_raw in ("seer", "medium", "knight") else None
        )
        addressed_raw = parsed.get("addressed_name")
        addressed_name: str | None = None
        if isinstance(addressed_raw, str):
            stripped = addressed_raw.strip()
            addressed_name = stripped or None
        return TextAnalysis(
            addressed_name=addressed_name,
            co_declaration=co_declaration,
        )

    @staticmethod
    def _parse_response(raw: str) -> dict:  # type: ignore[type-arg]
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            log.warning("gemini_text_json_parse_failed raw=%s", text[:200])
            return {}


__all__ = [
    "FakeTextAnalyzer",
    "GeminiTextAnalyzer",
    "TextAnalysis",
    "TextAnalyzer",
    "TextAnalyzerError",
]
