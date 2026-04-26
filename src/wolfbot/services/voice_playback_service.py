"""Discord voice channel playback Protocol + Fake.

NPC bots play TTS audio in `MAIN_VOICE_CHANNEL_ID` only after Master sends
`PlaybackAuthorized`. The Discord-side playback API is wrapped behind a
Protocol so unit tests use `FakeVoicePlayback`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class VoicePlaybackError(RuntimeError):
    """Playback failure that should surface via `playback_failed`."""

    def __init__(self, failure_reason: str) -> None:
        super().__init__(failure_reason)
        self.failure_reason = failure_reason


@runtime_checkable
class VoicePlayback(Protocol):
    async def play(self, *, audio: bytes, sample_rate: int) -> tuple[int, int]:
        """Play `audio`. Returns (started_at_ms, finished_at_ms)."""
        ...


@dataclass
class FakeVoicePlayback:
    """Captures playback calls and drives configurable timing for tests."""

    started_at_ms: int = 0
    finished_at_ms: int = 0
    raise_for_audio: Exception | None = None
    plays: list[tuple[bytes, int]] = field(default_factory=list)

    async def play(self, *, audio: bytes, sample_rate: int) -> tuple[int, int]:
        self.plays.append((audio, sample_rate))
        if self.raise_for_audio is not None:
            raise self.raise_for_audio
        return (self.started_at_ms, self.finished_at_ms)


@dataclass
class DiscordVoicePlayback:
    """Production playback wrapper.

    Lifts the actual play-into-VC operation into a user-supplied async
    callable so this module remains decoupled from `discord.py`.
    """

    play_fn: Callable[[bytes, int], Awaitable[tuple[int, int]]]

    async def play(self, *, audio: bytes, sample_rate: int) -> tuple[int, int]:
        return await self.play_fn(audio, sample_rate)


__all__ = [
    "DiscordVoicePlayback",
    "FakeVoicePlayback",
    "VoicePlayback",
    "VoicePlaybackError",
]
