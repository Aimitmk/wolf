"""TTS adapter Protocol + cost-minimized default skeleton.

The MVP TTS provider is the Google Cloud TTS Standard voices (per design.md),
but we want NPC bots to be configurable. Define a Protocol so production
plugs in any provider and tests substitute `FakeTtsService`.

Each NPC bot keeps a small in-memory cache keyed by `(provider, voice_id,
sha256(text), speed, pitch)` to avoid re-synthesizing the same utterance.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TtsRequest:
    text: str
    voice_id: str
    speed: float = 1.0
    pitch: float = 0.0
    language: str = "ja-JP"


@dataclass(frozen=True)
class TtsResult:
    audio: bytes
    duration_ms: int
    sample_rate: int = 48_000


class TtsProviderError(RuntimeError):
    """Hard TTS failure (timeout, 5xx, malformed). Carries a `failure_reason`."""

    def __init__(self, failure_reason: str) -> None:
        super().__init__(failure_reason)
        self.failure_reason = failure_reason


@runtime_checkable
class TtsService(Protocol):
    async def synthesize(self, req: TtsRequest) -> TtsResult: ...


class FakeTtsService:
    """In-memory TTS for tests."""

    def __init__(
        self,
        scripted: list[TtsResult | Exception] | None = None,
        default: TtsResult | None = None,
    ) -> None:
        self._scripted = list(scripted or [])
        self._default = default or TtsResult(audio=b"audio-fake", duration_ms=500)
        self.call_count = 0
        self.requests: list[TtsRequest] = []

    async def synthesize(self, req: TtsRequest) -> TtsResult:
        self.requests.append(req)
        self.call_count += 1
        if self._scripted:
            head = self._scripted.pop(0)
            if isinstance(head, Exception):
                raise head
            return head
        return self._default


class GoogleCloudTtsService:
    """Production cost-minimized adapter — delegates synthesis to a user-supplied callable.

    Like the STT adapter, this class deliberately does NOT import the
    `google-cloud-texttospeech` SDK at module-load time. The real bot wires
    a `synth_fn` that issues the actual API call.
    """

    def __init__(
        self,
        *,
        project: str,
        synth_fn: Callable[[TtsRequest], Awaitable[TtsResult]] | None = None,
    ) -> None:
        self.project = project
        self._synth_fn = synth_fn

    async def synthesize(self, req: TtsRequest) -> TtsResult:
        if self._synth_fn is None:
            raise TtsProviderError("tts_provider_not_configured")
        return await self._synth_fn(req)


class InMemoryTtsCache:
    """Bounded LRU cache for synthesized audio.

    The cache is per-process; a Master restart drops it. Keys hash on
    `(voice_id, text, speed, pitch)` so distinct utterances are kept
    distinct even if their text matches another voice's cache entry.
    """

    def __init__(self, *, max_entries: int = 256) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[str, TtsResult] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(req: TtsRequest) -> str:
        digest = hashlib.sha256(req.text.encode("utf-8")).hexdigest()
        return f"{req.voice_id}|{req.speed}|{req.pitch}|{digest}"

    def get(self, req: TtsRequest) -> TtsResult | None:
        key = self._key(req)
        result = self._entries.get(key)
        if result is None:
            self.misses += 1
            return None
        self.hits += 1
        # Re-insert to mark as recently used.
        self._entries.move_to_end(key)
        return result

    def put(self, req: TtsRequest, result: TtsResult) -> None:
        key = self._key(req)
        self._entries[key] = result
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


__all__ = [
    "FakeTtsService",
    "GoogleCloudTtsService",
    "InMemoryTtsCache",
    "TtsProviderError",
    "TtsRequest",
    "TtsResult",
    "TtsService",
]
