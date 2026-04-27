"""Shared persona base — types + pool-agnostic pickers.

`Persona` is the generic identity carrier (display name + judgment-style
guide + structured speech profile). Concrete persona instances live in:

* :mod:`wolfbot.npc.personas`    — player characters (Gnosia-flavored).
* :mod:`wolfbot.master.personas` — Master/GM narrator voices.

`SpeechProfile` holds the structured speech-reproduction data
(first-person, address style, signature phrases, narration mode) that the
system prompt's `## 話法` block consumes. Keep `style_guide` for
personality/judgment and `speech_profile` for 喋り方/語彙/文体 — do not
mix the two in free-form prose.

Some personas are near-silent in their source material (e.g. Kukrushka)
— `narration_mode="silent_gesture"` makes the prompt builder render
gesture descriptions instead of a normal conversation profile.

Hard rule: do NOT quote original source dialogue when defining a persona.
Only imitate temperament + register.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from random import Random
from typing import Literal


@dataclass(frozen=True)
class SpeechProfile:
    first_person: str
    self_reference_aliases: tuple[str, ...] = ()
    address_style: str = ""
    sentence_style: str = ""
    pause_style: str = ""
    signature_phrases: tuple[str, ...] = ()
    forbidden_overuse: tuple[str, ...] = ()
    narration_mode: Literal["standard", "silent_gesture"] = "standard"


@dataclass(frozen=True)
class JudgmentProfile:
    """Structured judgment-tendency axes consumed by the prompt builder.

    Each axis is 0.0-1.0. Values are rendered as labeled qualitative bands
    in the system prompt so the LLM has a concrete tendency to lean toward,
    independent of the free-form `style_guide` prose.

    Defaults are neutral so personas without explicit values still render
    cleanly. Overrides per persona shape behaviour:

    - `trust_hard_facts`: how much weight Master's HARD-confidence deductions
      get. Logical personas (Raqio) ≈ 1.0; defiant/wolf-leaning personas
      can dip to 0.7 to keep verbal cover for muddling logic.
    - `trust_medium_facts`: weight for MEDIUM-confidence deductions.
      Conservative seers and analyzers stay high (0.7-0.9); deceiver-leaning
      personas drop lower (0.3-0.5) to keep room for contrarian reads.
    - `contrarian_bias`: tendency to deliberately question majority view.
      Wolves & disruptors high; loyal villagers low.
    - `aggression`: speed of moving from suspicion to active accusation.
    - `bandwagon_tendency`: how readily the persona joins forming consensus.

    The system prompt rendering pairs these axes with explicit guidance —
    the persona doesn't have to compute them, just lean toward them.
    """

    trust_hard_facts: float = 1.0
    trust_medium_facts: float = 0.7
    contrarian_bias: float = 0.0
    aggression: float = 0.5
    bandwagon_tendency: float = 0.5


_DEFAULT_JUDGMENT_PROFILE = JudgmentProfile()


@dataclass(frozen=True)
class Persona:
    key: str
    display_name: str
    style_guide: str
    speech_profile: SpeechProfile
    judgment_profile: JudgmentProfile = _DEFAULT_JUDGMENT_PROFILE
    tts_voice_id: int | None = None


def index_by_key(pool: Sequence[Persona]) -> dict[str, Persona]:
    """Build a key→Persona index. Raises if keys collide within `pool`."""
    out: dict[str, Persona] = {}
    for p in pool:
        if p.key in out:
            raise ValueError(f"duplicate persona key {p.key!r} in pool")
        out[p.key] = p
    return out


def pick_personas(pool: Sequence[Persona], count: int, rng: Random) -> list[Persona]:
    """Pick `count` distinct personas from `pool` at random."""
    if count < 0 or count > len(pool):
        raise ValueError(f"cannot pick {count} personas; pool has {len(pool)}")
    return rng.sample(list(pool), count)


def pick_personas_excluding(
    pool: Sequence[Persona],
    count: int,
    exclude_keys: Sequence[str],
    rng: Random,
) -> list[Persona]:
    """Pick from `pool` minus `exclude_keys` — useful when you somehow need to extend."""
    excluded = set(exclude_keys)
    candidates = [p for p in pool if p.key not in excluded]
    if count > len(candidates):
        raise ValueError(f"cannot pick {count} personas; only {len(candidates)} available")
    return rng.sample(candidates, count)


__all__ = [
    "JudgmentProfile",
    "Persona",
    "SpeechProfile",
    "index_by_key",
    "pick_personas",
    "pick_personas_excluding",
]
