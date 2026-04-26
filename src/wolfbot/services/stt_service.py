"""Speech-to-text adapter Protocol + Gemini-backed implementation skeleton.

The MVP STT provider is the Gemini API audio-input feature (per the
voice-ingest spec). We define a Protocol so unit tests can substitute
`FakeSttService` without making real HTTP calls. The Gemini implementation
is intentionally a skeleton — it lifts configuration from env vars and
shows the call site, but cannot be exercised end-to-end without live API
credentials.
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
    """

    text: str
    confidence: float
    duration_ms: int


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
        transcribe_fn: Callable[[bytes, str, str, float], Awaitable[SttResult]] | None = None,
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


__all__ = [
    "FakeSttService",
    "GeminiSttService",
    "SttProviderError",
    "SttResult",
    "SttService",
]
