"""NPC bot worker settings.

Loaded once at startup via pydantic-settings.  The actual env file path
is selected by :mod:`wolfbot.npc.main` from ``WOLFBOT_NPC_ENV``; per-
persona templates live under ``envs/npc/.env.<persona>.example`` (see
:file:`envs/npc/README.md`).  One NPC worker process = one persona = one
``envs/npc/.env.<persona>`` file (each NPC needs its own Discord bot
token and a unique ``NPC_ID``).

Master-process settings live in :mod:`wolfbot.config`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from wolfbot.llm.decider_config import LLMDeciderConfig, LLMProvider


class NpcSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.npc", env_file_encoding="utf-8", extra="ignore"
    )

    # NPC identity / Discord
    NPC_ID: str
    NPC_DISCORD_TOKEN: SecretStr
    DISCORD_GUILD_ID: int
    MAIN_VOICE_CHANNEL_ID: int

    # Persona this NPC bot embodies. Must be a key from
    # wolfbot.npc.personas.NPC_PERSONAS_BY_KEY (e.g. "setsu", "gina", ...).
    # Each NPC bot process is bound to exactly one persona at startup.
    NPC_PERSONA_KEY: str

    # Master WS connection
    MASTER_WS_URL: str
    MASTER_NPC_PSK: SecretStr

    # ── NPC LLM ───────────────────────────────────────────────────────
    # The LLM that generates this NPC bot's *short reactive utterances*
    # during DAY_DISCUSSION in reactive_voice mode.  One ~80-character
    # in-character line at a time, given the current LogicPacket and
    # SpeakRequest from Master.
    #
    # Scope is intentionally narrow: this LLM does NOT decide votes,
    # night actions, or anything else — Master's GAMEPLAY_LLM_* (in
    # `.env.master`) handles those decisions for every LLM seat
    # regardless of mode.
    #
    # The provider switch mirrors GAMEPLAY_LLM_* on the Master side:
    # the same three providers (xai / deepseek / gemini), the same
    # field semantics, just under the NPC_LLM_* prefix.  This lets you
    # point the NPC LLM at a cheap fast model (e.g. xAI grok-4-1-fast)
    # while running the Gameplay LLM on a deeper-reasoning model (e.g.
    # Vertex Gemini), or any other split.
    NPC_LLM_PROVIDER: LLMProvider = "xai"

    # xAI / DeepSeek / any OpenAI-compatible endpoint.
    NPC_LLM_API_KEY: SecretStr | None = None
    NPC_LLM_MODEL: str = "grok-4-1-fast"
    # Override the provider default base URL when pointing at a
    # self-hosted OpenAI-compatible endpoint (vLLM, Ollama, ...).
    NPC_LLM_BASE_URL: str | None = None

    # DeepSeek-specific knobs.  Ignored unless provider == "deepseek".
    NPC_LLM_THINKING: Literal["enabled", "disabled"] = "enabled"
    NPC_LLM_REASONING_EFFORT: Literal["high", "max"] = "max"

    # Gemini Vertex AI specific.  Authentication is ADC/IAM only.
    NPC_LLM_VERTEX_PROJECT: str | None = None
    NPC_LLM_VERTEX_LOCATION: str = "global"
    NPC_LLM_THINKING_LEVEL: Literal["minimal", "low", "medium", "high"] = "high"

    # VOICEVOX TTS
    TTS_VOICE_ID: str = "3"
    VOICEVOX_URL: str = "http://localhost:50021"

    # Worker tunables
    HEARTBEAT_INTERVAL_S: float = 5.0
    LOG_LEVEL: str = "INFO"

    @model_validator(mode="after")
    def _require_npc_provider_key(self) -> NpcSettings:
        if self.NPC_LLM_PROVIDER in ("xai", "deepseek") and self.NPC_LLM_API_KEY is None:
            raise ValueError(
                f"NPC_LLM_PROVIDER={self.NPC_LLM_PROVIDER} requires NPC_LLM_API_KEY to be set"
            )
        # Mirror MasterSettings: gemini accepts either Vertex AI
        # (via NPC_LLM_VERTEX_PROJECT + ADC) or Google AI Studio
        # (via NPC_LLM_API_KEY in AIza... format). At least one must
        # be set; if both are present, Vertex wins.
        if (
            self.NPC_LLM_PROVIDER == "gemini"
            and not self.NPC_LLM_VERTEX_PROJECT
            and self.NPC_LLM_API_KEY is None
        ):
            raise ValueError(
                "NPC_LLM_PROVIDER=gemini requires either "
                "NPC_LLM_VERTEX_PROJECT (Vertex AI / ADC) or "
                "NPC_LLM_API_KEY (Google AI Studio)"
            )
        return self

    def npc_decider_config(self, *, timeout: float = 30.0) -> LLMDeciderConfig:
        """Project this Settings instance onto the provider-agnostic
        ``LLMDeciderConfig`` consumed by the NPC generator factory.
        """
        return LLMDeciderConfig(
            provider=self.NPC_LLM_PROVIDER,
            api_key=self.NPC_LLM_API_KEY,
            model=self.NPC_LLM_MODEL,
            base_url=self.NPC_LLM_BASE_URL,
            thinking=self.NPC_LLM_THINKING,
            reasoning_effort=self.NPC_LLM_REASONING_EFFORT,
            vertex_project=self.NPC_LLM_VERTEX_PROJECT,
            vertex_location=self.NPC_LLM_VERTEX_LOCATION,
            thinking_level=self.NPC_LLM_THINKING_LEVEL,
            timeout=timeout,
        )
