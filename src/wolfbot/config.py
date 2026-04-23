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
