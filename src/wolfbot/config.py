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
        env_file=".env.master", env_file_encoding="utf-8", extra="ignore"
    )

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

    # ── Voice STT provider switch ─────────────────────────────────────
    # ``gemini`` (default, legacy) — single multimodal call to Gemini
    # Flash that does transcription + structured analysis in one hop.
    # AI Studio's free-tier RPM is tight enough that a typical game
    # exhausts it almost immediately (observed 2026-04-28: every
    # segment 429'd).
    #
    # ``groq`` — two-step pipeline: Groq Whisper transcribes audio;
    # the analyzer LLM (xAI Grok by default, reusing
    # ``GAMEPLAY_LLM_*``) extracts CO claim, vote target,
    # ``addressed_name``, summary from the transcript. Whisper-large-v3
    # on Groq is ~$0.04-0.111/audio-hour with much higher RPM headroom.
    VOICE_STT_PROVIDER: Literal["gemini", "groq"] = "gemini"

    # ── Voice LLM (Gemini path) ───────────────────────────────────────
    # The multimodal LLM that *understands human voice* in VC — single
    # API call returns transcription + summary + CO detection + vote
    # target extraction.  This is a separate role from the Gameplay LLM
    # because it needs audio input (Gemini Flash via the AI Studio REST
    # API; not the OpenAI-compatible chat-completions surface).
    VOICE_LLM_API_KEY: SecretStr | None = None
    VOICE_LLM_MODEL: str = "gemini-2.0-flash-lite"

    # ── Groq Whisper STT (groq path) ──────────────────────────────────
    # Required when ``VOICE_STT_PROVIDER=groq``. The analyzer step
    # piggy-backs on ``GAMEPLAY_LLM_*`` (same xAI key + model), so no
    # separate analyzer config is needed in the typical setup.
    #
    # ``GROQ_STT_MODEL`` defaults to ``whisper-large-v3-turbo`` — the
    # cheapest multilingual Whisper variant on Groq that still handles
    # Japanese well; switch to ``whisper-large-v3`` for max accuracy at
    # ~3x the cost.
    GROQ_STT_API_KEY: SecretStr | None = None
    GROQ_STT_MODEL: str = "whisper-large-v3-turbo"
    GROQ_STT_BASE_URL: str = "https://api.groq.com/openai/v1"

    # ── Pre-STT silence gate ──────────────────────────────────────────
    # Discord's speaking-start fires on any audio above a low threshold
    # (breathing, keyboard, room hum). With ``SilenceGeneratorSink``
    # padding, this would burn one Groq + one xAI analyzer call for
    # every such non-speech burst. The gate suppresses the STT call
    # (and emits ``stt_failed reason=pre_stt_silence_gate`` so the
    # arbiter still finalises the segment) when the buffer is too
    # short or too quiet to plausibly contain speech. Tuned from
    # voicetest measurements: pure noise sits at RMS 0-100, faint /
    # distant speech 200-500, normal talking 1000+.
    # Set ``VOICE_PRE_STT_MIN_RMS=0`` to disable the gate entirely.
    VOICE_PRE_STT_MIN_RMS: int = 200
    VOICE_PRE_STT_MIN_DURATION_MS: int = 300

    # ── Master TTS narration (reactive_voice only) ────────────────────
    # When `LLM_DISCUSSION_MODE=reactive_voice` and Master is in VC,
    # phase-transition announcements (PHASE_CHANGE / MORNING / VICTORY
    # / EXECUTION headlines) are read aloud by Master via VOICEVOX. Long
    # content (vote tallies, role reveal) goes to the VC's text chat.
    # Default speaker 47 = ナースロボ_タイプT (machine-like polite).
    MASTER_TTS_VOICE_ID: int = 47
    # Reuse the NPC-side VOICEVOX HTTP engine. Single shared local engine
    # is the typical setup; override only if Master needs a different one.
    MASTER_VOICEVOX_URL: str = "http://localhost:50021"

    @model_validator(mode="after")
    def _require_gameplay_provider_key(self) -> MasterSettings:
        if self.GAMEPLAY_LLM_PROVIDER in ("xai", "deepseek") and self.GAMEPLAY_LLM_API_KEY is None:
            raise ValueError(
                f"GAMEPLAY_LLM_PROVIDER={self.GAMEPLAY_LLM_PROVIDER} "
                "requires GAMEPLAY_LLM_API_KEY to be set"
            )
        # Gemini supports two auth modes:
        #   * Vertex AI via ADC + GAMEPLAY_LLM_VERTEX_PROJECT
        #   * Google AI Studio via GAMEPLAY_LLM_API_KEY (AIza... format)
        # At least one must be set; if both are set, Vertex wins
        # (production deployments rely on attached-SA credentials).
        if (
            self.GAMEPLAY_LLM_PROVIDER == "gemini"
            and not self.GAMEPLAY_LLM_VERTEX_PROJECT
            and self.GAMEPLAY_LLM_API_KEY is None
        ):
            raise ValueError(
                "GAMEPLAY_LLM_PROVIDER=gemini requires either "
                "GAMEPLAY_LLM_VERTEX_PROJECT (Vertex AI / ADC) or "
                "GAMEPLAY_LLM_API_KEY (Google AI Studio)"
            )
        return self

    @model_validator(mode="after")
    def _require_voice_stt_provider_key(self) -> MasterSettings:
        if self.VOICE_STT_PROVIDER == "groq" and self.GROQ_STT_API_KEY is None:
            raise ValueError("VOICE_STT_PROVIDER=groq requires GROQ_STT_API_KEY to be set")
        # The Groq path's analyzer step reuses GAMEPLAY_LLM_*. The xAI/DeepSeek
        # case is already covered above; the Gemini case isn't fit for the
        # OpenAI-compatible analyzer call shape, so block that combo loud and
        # early rather than failing per-segment at runtime.
        if self.VOICE_STT_PROVIDER == "groq" and self.GAMEPLAY_LLM_PROVIDER == "gemini":
            raise ValueError(
                "VOICE_STT_PROVIDER=groq's analyzer step reuses GAMEPLAY_LLM_* "
                "but requires an OpenAI-compatible provider (xai or deepseek), "
                f"not {self.GAMEPLAY_LLM_PROVIDER}. "
                "Set VOICE_STT_PROVIDER=gemini (uses VOICE_LLM_API_KEY) when "
                "switching gameplay to Gemini."
            )
        return self

    def apply_phase_durations(self) -> None:
        """Initialize the global :class:`PhaseDurations` singleton from env.

        Called once during boot from :mod:`wolfbot.main` after the
        Settings instance is constructed. Subsequent runtime mutations
        (a future ``/wolf settings duration_factor ...`` slash command,
        for example) should call :func:`set_phase_durations` directly
        rather than re-running this method, so the env-derived values
        don't accidentally clobber a UI-driven override.

        See :class:`wolfbot.domain.durations.PhaseDurations.from_env`
        for the env-var contract (``WOLFBOT_PHASE_DURATION_FACTOR`` plus
        per-phase ``WOLFBOT_*_DURATION`` overrides).
        """
        from wolfbot.domain.durations import (
            PhaseDurations,
            set_phase_durations,
        )

        set_phase_durations(PhaseDurations.from_env())

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
