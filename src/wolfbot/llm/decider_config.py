"""Provider-agnostic LLM decider configuration.

The Discord bot has *two* roles that need to call a chat-completion-style
LLM that returns structured JSON:

* **Gameplay LLM** — drives Master's votes / night actions / rounds-mode
  discussion text on behalf of LLM seats.  Lives in
  :mod:`wolfbot.services.llm_service` and is configured via
  :class:`wolfbot.config.MasterSettings` (env prefix ``GAMEPLAY_LLM_*``).
* **NPC speech LLM** — drives one NPC bot's short reactive utterance in
  ``reactive_voice`` mode.  Lives in
  :mod:`wolfbot.npc.speech.openai_compatible_generator` (and the Vertex Gemini
  sibling) and is configured via :class:`wolfbot.npc.runtime.config.NpcSettings`
  (env prefix ``NPC_LLM_*``).

Both roles support the same three providers (``xai`` / ``deepseek`` /
``gemini``) and share this dataclass so the wiring code is identical.
The role-specific env-var prefix is the *only* difference between the
two settings classes; the runtime decider/generator factories take a
fully-populated :class:`LLMDeciderConfig` and stay role-blind.

Why a separate module instead of putting this on the deciders directly:
the Settings classes need to import this without pulling in heavy
runtime deps (``openai``, ``google-genai``, ``tenacity``).  Keeping it
in :mod:`wolfbot.llm` keeps the import graph cheap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr

LLMProvider = Literal["xai", "deepseek", "gemini", "mock"]


@dataclass(frozen=True)
class LLMDeciderConfig:
    """All knobs needed to construct any of the three provider deciders.

    Constructed by ``MasterSettings.gameplay_decider_config()`` and
    ``NpcSettings.npc_decider_config()``; consumed by
    :func:`wolfbot.services.llm_service.make_llm_decider` (gameplay) and
    :func:`wolfbot.npc.speech.generator_factory.make_npc_generator` (NPC).

    Provider gating: the relevant Settings ``model_validator`` guarantees
    that the field tied to the chosen provider is non-None / non-empty
    by the time we construct this — the asserts in the factories are
    documentation aids for mypy.

    The ``"mock"`` provider is a special offline-test mode: no credential
    is required, no network call is ever made, and the decider/generator
    factories return a deterministic stub. Used by integration test rigs
    that exercise the full Master + NPC pipeline without burning real
    LLM tokens.
    """

    provider: LLMProvider

    # Common (xAI + DeepSeek path; ignored by Gemini).
    api_key: SecretStr | None = None
    model: str = "grok-4-1-fast"
    base_url: str | None = None  # None ⇒ provider default

    # DeepSeek-specific (ignored by xAI / Gemini).
    thinking: Literal["enabled", "disabled"] = "enabled"
    reasoning_effort: Literal["high", "max"] = "max"

    # Gemini Vertex AI specific (ignored by xAI / DeepSeek).
    vertex_project: str | None = None
    vertex_location: str = "global"
    thinking_level: Literal["minimal", "low", "medium", "high"] = "high"

    timeout: float = 30.0


__all__ = ["LLMDeciderConfig", "LLMProvider"]
