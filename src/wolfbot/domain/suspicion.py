"""Public suspicion record — who suspects whom, at what level, with what reason.

Each `Suspicion` row is emitted alongside a `SpeechEvent` and persisted to
the `speech_suspicions` table. Subsequent prompts surface the immutable
history back so a player who later contradicts their own past suspicion
without an explicit `update_from_level` + `update_reason` is detectable.

The history is the village-side anti-fabrication mechanism: a wolf who
"forgot" they said someone was trustworthy yesterday can be caught by
the recorded mismatch between past and current statements. It is also
the data source for visualising the suspicion graph during discussion.

Pure: no I/O, no asyncio.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SuspicionLevel(StrEnum):
    """Four-step scale a speaker assigns to a target seat.

    The scale is deliberately coarse so an LLM can pick consistently
    over many phases. The values map onto natural-language register:

    - ``trust``:  この人は村陣営寄りだと感じる (white-leaning)
    - ``low``:    弱い違和感、まだ静観 (mild raised eyebrow)
    - ``medium``: はっきり怪しい、議論で詰めたい (clearly suspicious)
    - ``high``:   今日処刑したい第一候補 (prime lynch target)
    """

    TRUST = "trust"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Suspicion(BaseModel):
    """One suspicion datum attached to a SpeechEvent.

    Keep frozen so the persisted row is immutable in memory once
    constructed. The DB schema mirrors this shape.

    Anti-fabrication contract: when the speaker has previously declared
    a suspicion against the same ``target_seat`` and is now amending it,
    they MUST set ``update_from_level`` to the prior level and provide
    a non-empty ``update_reason``. Subsequent prompts surface the full
    history so an unannounced reversal (e.g. ``trust → high`` with
    update_from_level=null) is detectable as evidence of fabrication.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_seat: int = Field(ge=1, le=9)
    level: SuspicionLevel
    reason: str = Field(min_length=1, max_length=500)
    update_from_level: SuspicionLevel | None = Field(
        default=None,
        description=(
            "Previous level this suspicion is updating from. None on the "
            "first declaration against this target_seat. When non-null, "
            "``update_reason`` MUST also be non-null and explain the "
            "shift — silent reversals are anti-fabrication red flags."
        ),
    )
    update_reason: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Why the level changed from ``update_from_level`` to ``level``. "
            "Required when ``update_from_level`` is set."
        ),
    )


__all__ = ["Suspicion", "SuspicionLevel"]
