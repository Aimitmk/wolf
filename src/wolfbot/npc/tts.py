"""TTS adapter Protocol + pluggable provider implementations.

Providers:
- ``VoicevoxTtsService`` — local VOICEVOX engine (free, requires ``voicevox_engine``
  running on localhost). Default for MVP; swap to another provider by changing the
  ``TTS_PROVIDER`` env var.
- ``GoogleCloudTtsService`` — skeleton; delegates to a user-supplied callable.
- ``FakeTtsService`` — deterministic stub for tests.

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
        self._default = default or TtsResult(
            audio=b"audio-fake", duration_ms=500)
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


class VoicevoxTtsService:
    """VOICEVOX local engine adapter.

    Requires the VOICEVOX engine running at ``base_url`` (default
    ``http://localhost:50021``). The ``voice_id`` maps to a VOICEVOX
    speaker ID (int). The two-step API: ``audio_query`` → ``synthesis``.

    Does NOT import ``httpx`` at module-load time so test environments
    without a running VOICEVOX process still load this file.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:50021",
        default_speaker: int = 3,
        timeout_s: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_speaker = default_speaker
        self.timeout_s = timeout_s

    def _speaker_id(self, voice_id: str) -> int:
        try:
            return int(voice_id)
        except (ValueError, TypeError):
            return self.default_speaker

    async def synthesize(self, req: TtsRequest) -> TtsResult:
        import httpx

        speaker = self._speaker_id(req.voice_id)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                # Step 1: audio_query
                query_resp = await client.post(
                    f"{self.base_url}/audio_query",
                    params={"text": req.text, "speaker": speaker},
                )
                if query_resp.status_code != 200:
                    raise TtsProviderError(
                        f"voicevox_audio_query_failed_{query_resp.status_code}"
                    )
                query_json = query_resp.json()
                query_json["speedScale"] = req.speed
                query_json["pitchScale"] = req.pitch

                # Step 2: synthesis
                synth_resp = await client.post(
                    f"{self.base_url}/synthesis",
                    params={"speaker": speaker},
                    json=query_json,
                )
                if synth_resp.status_code != 200:
                    raise TtsProviderError(
                        f"voicevox_synthesis_failed_{synth_resp.status_code}"
                    )
                audio = synth_resp.content
                # VOICEVOX outputs 24kHz WAV by default; estimate duration from size.
                # WAV header is 44 bytes, 16-bit mono = 2 bytes/sample, 24kHz.
                sample_rate = 24_000
                data_bytes = max(0, len(audio) - 44)
                duration_ms = int(data_bytes / (sample_rate * 2) * 1000)
                return TtsResult(
                    audio=audio, duration_ms=duration_ms, sample_rate=sample_rate
                )
        except TtsProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise TtsProviderError("voicevox_timeout") from exc
        except httpx.ConnectError as exc:
            raise TtsProviderError("voicevox_connection_refused") from exc
        except Exception as exc:
            raise TtsProviderError(f"voicevox_unexpected_{type(exc).__name__}") from exc


__all__ = [
    "FakeTtsService",
    "GoogleCloudTtsService",
    "InMemoryTtsCache",
    "TtsProviderError",
    "TtsRequest",
    "TtsResult",
    "TtsService",
    "VoicevoxTtsService",
]
