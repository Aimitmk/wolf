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

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class NpcSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.npc", env_file_encoding="utf-8", extra="ignore")

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
    # regardless of mode.  The split lets you point the NPC LLM at a
    # cheaper / faster / different-tone model than the Gameplay LLM if
    # you want.
    #
    # Any OpenAI Chat Completions-compatible endpoint works; swap
    # providers by changing NPC_LLM_BASE_URL + NPC_LLM_MODEL (xAI Grok,
    # OpenAI, Groq, Together, vLLM, Ollama, ...).
    NPC_LLM_API_KEY: SecretStr
    NPC_LLM_MODEL: str = "grok-4-1-fast"
    NPC_LLM_BASE_URL: str = "https://api.x.ai/v1"

    # VOICEVOX TTS
    TTS_VOICE_ID: str = "3"
    VOICEVOX_URL: str = "http://localhost:50021"

    # Worker tunables
    HEARTBEAT_INTERVAL_S: float = 5.0
    LOG_LEVEL: str = "INFO"
