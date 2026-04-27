"""Master-process settings.

Loaded once at startup from ``.env.master`` via pydantic-settings.

NPC bot worker settings live in :mod:`wolfbot.npc.config`.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Any OpenAI Chat Completions-compatible endpoint works (xAI Grok,
    # OpenAI, Groq, Together, vLLM, Ollama, ...). The same credential
    # may be shared with NPC bots' NPC_LLM_API_KEY when convenient, but
    # the two roles are intentionally split so they can target different
    # models / providers.
    GAMEPLAY_LLM_API_KEY: SecretStr
    GAMEPLAY_LLM_MODEL: str = "grok-4-1-fast"

    # ── Master ↔ NPC bot / voice-ingest WebSocket transport ───────────
    MASTER_WS_LISTEN: str = "127.0.0.1:8800"
    MASTER_NPC_PSK: SecretStr | None = None

    # ── Voice LLM ─────────────────────────────────────────────────────
    # The multimodal LLM that *understands human voice* in VC — single
    # API call returns transcription + summary + CO detection + vote
    # target extraction.  Default targets Google Gemini Flash; swap via
    # env (the analyzer is wired in main.py for any compatible provider).
    VOICE_LLM_API_KEY: SecretStr | None = None
    VOICE_LLM_MODEL: str = "gemini-2.0-flash-lite"
