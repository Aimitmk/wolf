"""Environment-driven settings.

Loaded once at startup from `.env` via pydantic_settings.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DISCORD_TOKEN: SecretStr
    XAI_API_KEY: SecretStr
    XAI_MODEL: str = "grok-4-1-fast"
    DISCORD_GUILD_ID: int
    MAIN_TEXT_CHANNEL_ID: int
    MAIN_VOICE_CHANNEL_ID: int
    WOLFBOT_DB_PATH: str = "./wolfbot.db"
    LOG_LEVEL: str = "INFO"
    # Discussion mode applied to NEW games started under this process.
    # Existing rows keep whichever mode was captured at their start time.
    LLM_DISCUSSION_MODE: str = "rounds"
    # Master ↔ NPC bot / voice-ingest WebSocket transport.
    MASTER_WS_LISTEN: str = "127.0.0.1:8800"
    MASTER_NPC_PSK: SecretStr | None = None
