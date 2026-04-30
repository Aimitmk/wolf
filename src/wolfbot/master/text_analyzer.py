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

from wolfbot.domain.enums import (
    CO_CLAIM_VALUES,
    ROLE_CALLOUT_VALUES,
    format_co_claim_options,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TextAnalysis:
    """Structured analysis of a single text-channel utterance."""

    addressed_name: str | None = None
    co_declaration: str | None = None
    # Role the utterance is calling for (e.g. "占い師の方どうぞ" → "seer").
    # None for non-callouts. Mirrors `SttResult.role_callout`.
    role_callout: str | None = None


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
        '  "addressed_name": null,\n'
        '  "role_callout": null\n'
        "}\n"
        "```\n\n"
        "フィールド説明:\n"
        f"- co_claim: 役職CO(自称)があれば {format_co_claim_options()}、なければ null\n"
        "- addressed_name: 特定のプレイヤーへの呼びかけがあればその名前(例 \"セツ\"、\"ジーナさん\"、\"席3\"、\"3番\")、なければ null。"
        "「みんな」「全員」など全体への呼びかけは null。さん/くん/ちゃん 等の敬称は付けたままでも構わない。"
        "発言内で他プレイヤーに言及するだけ(例: 『セツの判定が気になる』)は呼びかけではないので null。"
        "明確な宛先のある呼びかけ(例: 『セツさん、どう思う』)のみ設定。\n"
        "- role_callout: 役職への名乗り出を求める呼びかけ、または一般的な情報請求があれば "
        "\"seer\"/\"medium\"/\"knight\"/\"info_request\" のいずれか、なければ null。"
        "特定役職を名指しした呼びかけ (例「占い師の方は名乗り出てください」「霊媒師いますか?」「騎士は誰?」) → 該当役職。"
        "役職を限定しない一般的な情報請求 (例「誰か怪しい人いる?」「みんな意見を聞かせて」「気になる人を挙げて」"
        "「誰か役職持ち出てきて」「みんなどう思う?」「初日だけど何か情報ない?」) → \"info_request\"。"
        "ただし役職名を単に話題にしただけ (例「占い師の判定が気になる」「シゲミチが霊媒主張した」) は null。"
        "個人への質問 (例「セツさん、どう思う?」) は addressed_name 側で扱い、role_callout は null。"
        "全員/全体への問いかけで意見・情報・怪しい相手を求めているときに \"info_request\" を立てる。"
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

        from wolfbot.services.llm_trace import (
            CallTimer,
            extract_gemini_rest_tokens,
            log_llm_call,
        )

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
        timer = CallTimer()
        raw_text = ""
        tokens: dict[str, int | None] | None = None
        err: str | None = None
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                resp = await client.post(url, json=body)
                if resp.status_code != 200:
                    err = f"gemini_http_{resp.status_code}"
                    await log_llm_call(
                        role="text_analysis",
                        provider="gemini",
                        model=self.model,
                        system_prompt=self._SYSTEM_PROMPT,
                        user_prompt=text,
                        response=None,
                        latency_ms=timer.elapsed_ms,
                        error=err,
                    )
                    raise TextAnalyzerError(err)
                resp_json = resp.json()
                raw_text = (
                    resp_json.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
                tokens = extract_gemini_rest_tokens(resp_json)
                parsed = self._parse_response(raw_text)
        except TextAnalyzerError:
            raise
        except httpx.TimeoutException as exc:
            err = "gemini_timeout"
            await log_llm_call(
                role="text_analysis",
                provider="gemini",
                model=self.model,
                system_prompt=self._SYSTEM_PROMPT,
                user_prompt=text,
                response=None,
                latency_ms=timer.elapsed_ms,
                error=err,
            )
            raise TextAnalyzerError(err) from exc
        except httpx.ConnectError as exc:
            err = "gemini_connection_refused"
            await log_llm_call(
                role="text_analysis",
                provider="gemini",
                model=self.model,
                system_prompt=self._SYSTEM_PROMPT,
                user_prompt=text,
                response=None,
                latency_ms=timer.elapsed_ms,
                error=err,
            )
            raise TextAnalyzerError(err) from exc
        except Exception as exc:
            err = f"gemini_unexpected_{type(exc).__name__}"
            await log_llm_call(
                role="text_analysis",
                provider="gemini",
                model=self.model,
                system_prompt=self._SYSTEM_PROMPT,
                user_prompt=text,
                response=None,
                latency_ms=timer.elapsed_ms,
                error=err,
            )
            raise TextAnalyzerError(err) from exc

        await log_llm_call(
            role="text_analysis",
            provider="gemini",
            model=self.model,
            system_prompt=self._SYSTEM_PROMPT,
            user_prompt=text,
            response=raw_text,
            latency_ms=timer.elapsed_ms,
            error=None,
            tokens=tokens,
        )

        co_raw = parsed.get("co_claim")
        co_declaration = co_raw if co_raw in CO_CLAIM_VALUES else None
        addressed_raw = parsed.get("addressed_name")
        addressed_name: str | None = None
        if isinstance(addressed_raw, str):
            stripped = addressed_raw.strip()
            addressed_name = stripped or None
        callout_raw = parsed.get("role_callout")
        role_callout = (
            callout_raw if callout_raw in ROLE_CALLOUT_VALUES else None
        )
        return TextAnalysis(
            addressed_name=addressed_name,
            co_declaration=co_declaration,
            role_callout=role_callout,
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


class OpenAICompatibleTextAnalyzer:
    """OpenAI Chat Completions-compatible text analyzer.

    Mirrors :class:`GeminiTextAnalyzer` but POSTs to an OpenAI-compatible
    ``/chat/completions`` endpoint (xAI Grok / OpenAI / Together / vLLM
    / Ollama / etc.). Used by `main.py` when
    ``VOICE_STT_PROVIDER=groq``: in that mode the voice path already
    splits STT (Groq Whisper) from the analyzer step (xAI Grok via the
    gameplay LLM key), and the text path should follow the same split
    instead of round-tripping through Gemini. Without this, a user with
    Groq-mode voice still saw their typed messages fail with
    ``gemini_http_429`` whenever the shared Gemini key got rate-limited.

    Schema and prompt are identical to :class:`GeminiTextAnalyzer` so
    the two providers are interchangeable from the caller's POV.
    """

    _SYSTEM_PROMPT: str = GeminiTextAnalyzer._SYSTEM_PROMPT

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.x.ai/v1",
        timeout_s: float = 8.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    async def analyze(self, *, text: str, timeout_s: float) -> TextAnalysis:
        import httpx

        from wolfbot.services.llm_trace import (
            CallTimer,
            log_llm_call,
        )

        def _usage_from_dict(
            resp_json: dict[str, object],
        ) -> dict[str, int | None] | None:
            usage = resp_json.get("usage")
            if not isinstance(usage, dict):
                return None
            def _int_or_none(v: object) -> int | None:
                return int(v) if isinstance(v, int) else None
            return {
                "prompt": _int_or_none(usage.get("prompt_tokens")),
                "completion": _int_or_none(usage.get("completion_tokens")),
                "total": _int_or_none(usage.get("total_tokens")),
            }

        effective_timeout = min(timeout_s, self.timeout_s)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": f"発言:\n{text}"},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timer = CallTimer()
        raw_text = ""
        tokens: dict[str, int | None] | None = None
        err: str | None = None
        provider_tag = "openai-compat"
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                resp = await client.post(url, json=body, headers=headers)
                if resp.status_code != 200:
                    err = f"openai_http_{resp.status_code}"
                    await log_llm_call(
                        role="text_analysis",
                        provider=provider_tag,
                        model=self.model,
                        system_prompt=self._SYSTEM_PROMPT,
                        user_prompt=text,
                        response=None,
                        latency_ms=timer.elapsed_ms,
                        error=err,
                    )
                    raise TextAnalyzerError(err)
                resp_json = resp.json()
                raw_text = (
                    resp_json.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                ) or ""
                tokens = _usage_from_dict(resp_json)
                parsed = GeminiTextAnalyzer._parse_response(raw_text)
        except TextAnalyzerError:
            raise
        except httpx.TimeoutException as exc:
            err = "openai_timeout"
            await log_llm_call(
                role="text_analysis",
                provider=provider_tag,
                model=self.model,
                system_prompt=self._SYSTEM_PROMPT,
                user_prompt=text,
                response=None,
                latency_ms=timer.elapsed_ms,
                error=err,
            )
            raise TextAnalyzerError(err) from exc
        except httpx.ConnectError as exc:
            err = "openai_connection_refused"
            await log_llm_call(
                role="text_analysis",
                provider=provider_tag,
                model=self.model,
                system_prompt=self._SYSTEM_PROMPT,
                user_prompt=text,
                response=None,
                latency_ms=timer.elapsed_ms,
                error=err,
            )
            raise TextAnalyzerError(err) from exc
        except Exception as exc:
            err = f"openai_unexpected_{type(exc).__name__}"
            await log_llm_call(
                role="text_analysis",
                provider=provider_tag,
                model=self.model,
                system_prompt=self._SYSTEM_PROMPT,
                user_prompt=text,
                response=None,
                latency_ms=timer.elapsed_ms,
                error=err,
            )
            raise TextAnalyzerError(err) from exc

        await log_llm_call(
            role="text_analysis",
            provider=provider_tag,
            model=self.model,
            system_prompt=self._SYSTEM_PROMPT,
            user_prompt=text,
            response=raw_text,
            latency_ms=timer.elapsed_ms,
            error=None,
            tokens=tokens,
        )

        co_raw = parsed.get("co_claim")
        co_declaration = co_raw if co_raw in CO_CLAIM_VALUES else None
        addressed_raw = parsed.get("addressed_name")
        addressed_name: str | None = None
        if isinstance(addressed_raw, str):
            stripped = addressed_raw.strip()
            addressed_name = stripped or None
        callout_raw = parsed.get("role_callout")
        role_callout = (
            callout_raw if callout_raw in ROLE_CALLOUT_VALUES else None
        )
        return TextAnalysis(
            addressed_name=addressed_name,
            co_declaration=co_declaration,
            role_callout=role_callout,
        )


__all__ = [
    "FakeTextAnalyzer",
    "GeminiTextAnalyzer",
    "OpenAICompatibleTextAnalyzer",
    "TextAnalysis",
    "TextAnalyzer",
    "TextAnalyzerError",
]
