"""Speech-to-text adapter Protocol + pluggable provider implementations.

Providers:
- ``GeminiAudioAnalyzer`` — sends raw audio to a cheap Gemini model
  (e.g. gemini-2.0-flash-lite) and gets back transcription + structured
  analysis (summary, claimed role, vote target, stance) in one API call.
  This is the default for voice-ingest because it eliminates a separate
  STT + LLM hop.
- ``GeminiSttService`` — skeleton; delegates to a user-supplied callable.
- ``FakeSttService`` — deterministic stub for tests.

The ``SttService`` Protocol is the injection seam used by
``VoiceIngestService``. Any implementation satisfying the Protocol can be
swapped in via configuration.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SttResult:
    """Outcome of a single STT call.

    `text` is empty on a low-confidence drop; `confidence` is set so callers
    can apply their own threshold check before deciding to emit a SpeechEvent.
    Hard provider failures raise SttProviderError instead.

    `summary` is an optional structured analysis of the utterance content,
    populated by providers that combine STT + inference in one call (e.g.
    GeminiAudioAnalyzer). Providers that only do transcription leave it None.
    """

    text: str
    confidence: float
    duration_ms: int
    summary: str | None = None


class SttProviderError(RuntimeError):
    """Raised on hard STT failures (timeout, 5xx, malformed response).

    Carries a `failure_reason` matching the canonical voice-ingest enum
    (`stt_provider_error`, `stt_timeout`, etc.).
    """

    def __init__(self, failure_reason: str) -> None:
        super().__init__(failure_reason)
        self.failure_reason = failure_reason


@runtime_checkable
class SttService(Protocol):
    """Async STT adapter.

    Implementations MUST be cancellable and MUST NOT block the asyncio loop
    on the network call (use `asyncio.to_thread` or an async HTTP client).
    """

    async def transcribe(
        self,
        *,
        audio: bytes,
        language: str,
        timeout_s: float,
    ) -> SttResult: ...


class FakeSttService:
    """In-memory STT for tests.

    Either return a scripted sequence of results or raise scripted errors.
    """

    def __init__(
        self,
        scripted: list[SttResult | Exception] | None = None,
        default: SttResult | None = None,
    ) -> None:
        self._scripted: list[SttResult | Exception] = list(scripted or [])
        self._default: SttResult | None = default
        self.call_count = 0

    async def transcribe(
        self,
        *,
        audio: bytes,
        language: str,
        timeout_s: float,
    ) -> SttResult:
        self.call_count += 1
        if self._scripted:
            head = self._scripted.pop(0)
            if isinstance(head, Exception):
                raise head
            return head
        if self._default is None:
            raise SttProviderError("stt_no_script")
        return self._default


class GeminiSttService:
    """Production Gemini API STT adapter.

    The real API call is delegated to a user-supplied `transcribe_fn` so
    the actual transport (Google Generative SDK or raw HTTP) is configurable
    and the hot path stays test-friendly. This module deliberately does NOT
    import the `google.generativeai` SDK at module level so test environments
    without those credentials still load this file.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        transcribe_fn: Callable[[bytes, str, str, float],
                                Awaitable[SttResult]] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._transcribe_fn = transcribe_fn

    async def transcribe(
        self,
        *,
        audio: bytes,
        language: str,
        timeout_s: float,
    ) -> SttResult:
        if self._transcribe_fn is None:
            raise SttProviderError("stt_provider_not_configured")
        return await self._transcribe_fn(audio, language, self.model, timeout_s)


class GeminiAudioAnalyzer:
    """Gemini multimodal audio → transcription + structured analysis.

    Sends raw PCM/WAV audio directly to a cheap Gemini model and asks for
    both transcription and a structured JSON analysis in a single request.
    This replaces separate STT → LLM hops with one API call.

    The structured output includes:
    - ``transcript``: verbatim Japanese text
    - ``summary``: 1-sentence gist of what the speaker said
    - ``confidence``: self-assessed transcription confidence (0.0-1.0)
    - ``co_claim``: role CO if any (``seer``/``medium``/``knight``/null)
    - ``vote_target_seat``: seat number the speaker wants to execute (null if none)
    - ``stance``: dict of seat → trust (``positive``/``negative``/``neutral``)

    Uses ``httpx`` for the Gemini REST API to stay async-native. Model
    defaults to ``gemini-2.0-flash-lite`` (cheapest multimodal, ~$0.075/1M
    input tokens). Does NOT import ``httpx`` at module level.
    """

    _SYSTEM_PROMPT: str = (
        "あなたは人狼ゲームの音声ログ分析エンジンです。\n"
        "渡された音声(日本語)を書き起こし、以下のJSON形式で返してください。\n"
        "JSONのみ返答し、他のテキストは含めないでください。\n\n"
        "```json\n"
        "{\n"
        '  "transcript": "発話の書き起こし全文",\n'
        '  "summary": "1文の要約(30文字以内)",\n'
        '  "confidence": 0.95,\n'
        '  "co_claim": null,\n'
        '  "vote_target_seat": null,\n'
        '  "stance": {}\n'
        "}\n"
        "```\n\n"
        "フィールド説明:\n"
        "- transcript: 音声の書き起こし全文(日本語)\n"
        "- summary: 発言内容の1文要約\n"
        "- confidence: 書き起こし精度の自己評価(0.0〜1.0)\n"
        "- co_claim: 役職CO(自称)があれば \"seer\"/\"medium\"/\"knight\"、なければ null\n"
        "- vote_target_seat: 処刑対象として名指しした席番号(1〜9)、なければ null\n"
        "- stance: 言及した席への態度 {\"席番号\": \"positive\"/\"negative\"/\"neutral\"}\n"
        "\n音声が不明瞭な場合は confidence を低くし、transcript は聞き取れた範囲で。"
    )

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.0-flash-lite",
        api_base: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_s: float = 15.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.timeout_s = timeout_s

    async def transcribe(
        self,
        *,
        audio: bytes,
        language: str,
        timeout_s: float,
    ) -> SttResult:
        import base64
        import json

        import httpx

        audio_b64 = base64.b64encode(audio).decode("ascii")
        effective_timeout = min(timeout_s, self.timeout_s)

        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "audio/wav",
                                "data": audio_b64,
                            }
                        },
                        {
                            "text": self._SYSTEM_PROMPT,
                        },
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
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
                    raise SttProviderError(
                        f"gemini_http_{resp.status_code}"
                    )

                resp_json = resp.json()
                raw_text = (
                    resp_json.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )

                parsed = self._parse_response(raw_text)
                transcript = parsed.get("transcript", "")
                confidence = float(parsed.get("confidence", 0.0))

                # Build a compact summary JSON from the structured fields
                summary_dict = {
                    k: v
                    for k, v in parsed.items()
                    if k not in ("transcript", "confidence") and v is not None and v != {}
                }
                summary_str = json.dumps(
                    summary_dict, ensure_ascii=False) if summary_dict else None

                # Estimate duration from audio size (assume 16kHz 16-bit mono WAV)
                data_bytes = max(0, len(audio) - 44)
                duration_ms = int(data_bytes / (16_000 * 2) * 1000)

                return SttResult(
                    text=transcript,
                    confidence=confidence,
                    duration_ms=duration_ms,
                    summary=summary_str,
                )

        except SttProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise SttProviderError("gemini_timeout") from exc
        except httpx.ConnectError as exc:
            raise SttProviderError("gemini_connection_refused") from exc
        except Exception as exc:
            raise SttProviderError(
                f"gemini_unexpected_{type(exc).__name__}"
            ) from exc

    @staticmethod
    def _parse_response(raw: str) -> dict:  # type: ignore[type-arg]
        """Best-effort parse of Gemini's JSON response.

        Gemini sometimes wraps the JSON in markdown fences or adds
        trailing text. We strip those and try ``json.loads``.
        """
        import json

        text = raw.strip()
        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            # Drop first and last fence lines
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            log.warning("gemini_json_parse_failed raw=%s", text[:200])
            return {"transcript": text, "confidence": 0.3}


__all__ = [
    "FakeSttService",
    "GeminiAudioAnalyzer",
    "GeminiSttService",
    "SttProviderError",
    "SttResult",
    "SttService",
]
