"""Master-process settings.

Loaded once at startup from ``.env.master`` via pydantic-settings.

NPC bot worker settings live in :mod:`wolfbot.npc.config`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from wolfbot.llm.decider_config import LLMDeciderConfig, LLMProvider


class MasterSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.master", env_file_encoding="utf-8", extra="ignore")

    DISCORD_TOKEN: SecretStr
    DISCORD_GUILD_ID: int
    MAIN_TEXT_CHANNEL_ID: int
    MAIN_VOICE_CHANNEL_ID: int
    WOLFBOT_DB_PATH: str = "./wolfbot.db"
    LOG_LEVEL: str = "INFO"
    # Discussion mode applied to NEW games started under this process.
    # Existing rows keep whichever mode was captured at their start time.
    LLM_DISCUSSION_MODE: str = "rounds"

    # ── Gameplay LLM ───────────────────────────────────────────────────
    # The LLM that drives every gameplay decision Master makes on behalf
    # of LLM-controlled seats:
    #
    #   * Day-time votes (which seat to execute).
    #   * Night actions (wolf attack target, seer divine target, knight
    #     guard target).
    #   * Day-discussion text **in rounds mode** (Master generates the
    #     LLM seat's discussion turn directly).  In reactive_voice mode
    #     the discussion turns are produced by NPC bot processes via
    #     `wolfbot.npc.*` and use NPC_LLM_* there — but votes / night
    #     actions still flow through this Gameplay LLM.
    #
    # The provider switch is shared with NPC bots (same three providers,
    # same field semantics, just a different env-var prefix).  The two
    # roles can target completely different providers / models — e.g.
    # Gameplay on Vertex Gemini for deeper reasoning, NPC on xAI Grok
    # for cheap fast utterances.
    GAMEPLAY_LLM_PROVIDER: LLMProvider = "xai"

    # xAI / DeepSeek / any OpenAI-compatible endpoint.
    GAMEPLAY_LLM_API_KEY: SecretStr | None = None
    GAMEPLAY_LLM_MODEL: str = "grok-4-1-fast"
    # Override the provider default base URL when pointing at a
    # self-hosted OpenAI-compatible endpoint (vLLM, Ollama, ...).
    GAMEPLAY_LLM_BASE_URL: str | None = None

    # DeepSeek-specific knobs.  Ignored unless provider == "deepseek".
    GAMEPLAY_LLM_THINKING: Literal["enabled", "disabled"] = "enabled"
    GAMEPLAY_LLM_REASONING_EFFORT: Literal["high", "max"] = "max"

    # Gemini Vertex AI specific.  Authentication is ADC/IAM only
    # (gcloud locally; attached service account in prod).  Vertex AI
    # Express mode and API-key auth are deliberately unsupported.
    GAMEPLAY_LLM_VERTEX_PROJECT: str | None = None
    GAMEPLAY_LLM_VERTEX_LOCATION: str = "global"
    GAMEPLAY_LLM_THINKING_LEVEL: Literal["minimal", "low", "medium", "high"] = "high"

    # ── Master ↔ NPC bot / voice-ingest WebSocket transport ───────────
    MASTER_WS_LISTEN: str = "127.0.0.1:8800"
    MASTER_NPC_PSK: SecretStr | None = None

    # ── Voice LLM ─────────────────────────────────────────────────────
    # The multimodal LLM that *understands human voice* in VC — single
    # API call returns transcription + summary + CO detection + vote
    # target extraction.  This is a separate role from the Gameplay LLM
    # because it needs audio input (Gemini Flash via the AI Studio REST
    # API; not the OpenAI-compatible chat-completions surface).
    VOICE_LLM_API_KEY: SecretStr | None = None
    VOICE_LLM_MODEL: str = "gemini-2.0-flash-lite"

    @model_validator(mode="after")
    def _require_gameplay_provider_key(self) -> MasterSettings:
        if (
            self.GAMEPLAY_LLM_PROVIDER in ("xai", "deepseek")
            and self.GAMEPLAY_LLM_API_KEY is None
        ):
            raise ValueError(
                f"GAMEPLAY_LLM_PROVIDER={self.GAMEPLAY_LLM_PROVIDER} "
                "requires GAMEPLAY_LLM_API_KEY to be set"
            )
        if (
            self.GAMEPLAY_LLM_PROVIDER == "gemini"
            and not self.GAMEPLAY_LLM_VERTEX_PROJECT
        ):
            raise ValueError(
                "GAMEPLAY_LLM_PROVIDER=gemini requires "
                "GAMEPLAY_LLM_VERTEX_PROJECT to be set"
            )
        return self

    def gameplay_decider_config(self, *, timeout: float = 30.0) -> LLMDeciderConfig:
        """Project this Settings instance onto the provider-agnostic
        ``LLMDeciderConfig`` consumed by the decider factory.
        """
        return LLMDeciderConfig(
            provider=self.GAMEPLAY_LLM_PROVIDER,
            api_key=self.GAMEPLAY_LLM_API_KEY,
            model=self.GAMEPLAY_LLM_MODEL,
            base_url=self.GAMEPLAY_LLM_BASE_URL,
            thinking=self.GAMEPLAY_LLM_THINKING,
            reasoning_effort=self.GAMEPLAY_LLM_REASONING_EFFORT,
            vertex_project=self.GAMEPLAY_LLM_VERTEX_PROJECT,
            vertex_location=self.GAMEPLAY_LLM_VERTEX_LOCATION,
            thinking_level=self.GAMEPLAY_LLM_THINKING_LEVEL,
            timeout=timeout,
        )
