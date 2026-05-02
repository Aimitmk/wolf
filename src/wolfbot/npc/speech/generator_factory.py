"""Provider-aware NPC speech generator factory.

Mirror of :func:`wolfbot.services.llm_service.make_llm_decider`, but for
the NPC speech path: takes a provider-agnostic
:class:`wolfbot.llm.decider_config.LLMDeciderConfig` plus the persona
key bound to the worker process at startup, and returns a fully
configured :class:`wolfbot.npc.speech.speech_service.NpcGenerator`.

Three providers supported, identical to the gameplay LLM path:

* ``xai`` — :class:`OpenAICompatibleNpcGenerator` in default
  ``json_schema`` strict mode against any OpenAI-compatible endpoint.
* ``deepseek`` — :class:`OpenAICompatibleNpcGenerator` in
  ``json_object`` mode with the JSON contract suffix appended and
  ``thinking`` / ``reasoning_effort`` forwarded via ``extra_body``.
* ``gemini`` — :class:`GeminiNpcGenerator` against Vertex AI with
  ADC/IAM auth.

The Settings ``model_validator`` that built the ``LLMDeciderConfig``
guarantees the relevant credential is non-None / non-empty by the time
we get here; the asserts below are documentation aids for mypy.
"""

from __future__ import annotations

from wolfbot.llm.decider_config import LLMDeciderConfig
from wolfbot.npc.speech.speech_service import NpcGenerator


def make_npc_generator(cfg: LLMDeciderConfig, *, persona_key: str) -> NpcGenerator:
    if cfg.provider == "xai":
        from wolfbot.npc.speech.openai_compatible_generator import (
            OpenAICompatibleConfig,
            OpenAICompatibleNpcGenerator,
        )

        assert cfg.api_key is not None  # validated in NpcSettings
        gen = OpenAICompatibleNpcGenerator(
            api_key=cfg.api_key.get_secret_value(),
            config=OpenAICompatibleConfig(
                model=cfg.model,
                base_url=cfg.base_url or "https://api.x.ai/v1",
                timeout=cfg.timeout,
                mode="json_schema",
            ),
        )
        gen.set_persona(persona_key)
        return gen

    if cfg.provider == "deepseek":
        from wolfbot.npc.speech.openai_compatible_generator import (
            OpenAICompatibleConfig,
            OpenAICompatibleNpcGenerator,
        )

        assert cfg.api_key is not None  # validated in NpcSettings
        gen_ds = OpenAICompatibleNpcGenerator(
            api_key=cfg.api_key.get_secret_value(),
            config=OpenAICompatibleConfig(
                model=cfg.model,
                base_url=cfg.base_url or "https://api.deepseek.com",
                timeout=cfg.timeout,
                mode="json_object",
                thinking=cfg.thinking,
                reasoning_effort=cfg.reasoning_effort,
            ),
        )
        gen_ds.set_persona(persona_key)
        return gen_ds

    if cfg.provider == "gemini":
        from wolfbot.npc.speech.gemini_generator import (
            GeminiNpcGenerator,
            GeminiVertexConfig,
        )

        # Vertex (project) wins when both are set; AI Studio (api_key)
        # is the fallback. NpcSettings.validator guarantees at least
        # one is present.
        api_key_value: str | None = (
            cfg.api_key.get_secret_value() if cfg.api_key is not None else None
        )
        gen_gemini = GeminiNpcGenerator(
            config=GeminiVertexConfig(
                project=cfg.vertex_project,
                location=cfg.vertex_location,
                model=cfg.model,
                thinking_level=cfg.thinking_level,
                timeout=cfg.timeout,
                api_key=api_key_value if not cfg.vertex_project else None,
            ),
        )
        gen_gemini.set_persona(persona_key)
        return gen_gemini

    if cfg.provider == "mock":
        from wolfbot.npc.speech.mock_generator import MockNpcGenerator

        gen_mock = MockNpcGenerator()
        gen_mock.set_persona(persona_key)
        return gen_mock

    raise ValueError(f"unknown provider: {cfg.provider!r}")


__all__ = ["make_npc_generator"]
