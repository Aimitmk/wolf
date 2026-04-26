"""Environment-driven settings.

Loaded once at startup from `.env` via pydantic_settings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DISCORD_TOKEN: SecretStr
    DISCORD_GUILD_ID: int
    MAIN_TEXT_CHANNEL_ID: int
    MAIN_VOICE_CHANNEL_ID: int
    WOLFBOT_DB_PATH: str = "./wolfbot.db"
    LOG_LEVEL: str = "INFO"

    LLM_PROVIDER: Literal["xai", "deepseek"] = "xai"

    XAI_API_KEY: SecretStr | None = None
    XAI_MODEL: str = "grok-4-1-fast"

    DEEPSEEK_API_KEY: SecretStr | None = None
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-v4-flash"
    DEEPSEEK_THINKING: Literal["enabled", "disabled"] = "enabled"
    DEEPSEEK_REASONING_EFFORT: Literal["high", "max"] = "max"

    @model_validator(mode="after")
    def _require_provider_key(self) -> Settings:
        if self.LLM_PROVIDER == "xai" and self.XAI_API_KEY is None:
            raise ValueError("LLM_PROVIDER=xai requires XAI_API_KEY to be set")
        if self.LLM_PROVIDER == "deepseek" and self.DEEPSEEK_API_KEY is None:
            raise ValueError("LLM_PROVIDER=deepseek requires DEEPSEEK_API_KEY to be set")
        return self
