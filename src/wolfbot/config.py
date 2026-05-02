"""Master-process settings.

Loaded once at startup from ``.env.master`` via pydantic-settings.

NPC bot worker settings live in :mod:`wolfbot.npc.runtime.config`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from wolfbot.llm.decider_config import LLMDeciderConfig, LLMProvider

# Pipeline-shape literals ────────────────────────────────────────────────
# Kept at module scope because they participate in cross-field validators
# and the test suite imports them directly when constructing minimal
# Settings instances.

VoicePipeline = Literal["audio_analyzer", "stt_then_text_analyzer", "disabled"]
"""How human voice is converted into a structured ``SpeechEvent``:

* ``audio_analyzer`` — single multimodal call (audio → transcript +
  structured fields). Currently only Gemini Flash supports this shape.
* ``stt_then_text_analyzer`` — two-step pipeline. ``Stt`` transcribes
  audio (Groq Whisper). ``TextAnalyzer`` extracts structured fields from
  the transcript. The same ``TextAnalyzer`` instance is shared with the
  text channel path so they hit one credential pool.
* ``disabled`` — Master does not join VC, no voice ingest. Used by
  ``rounds`` mode and by reactive_voice setups that only have NPCs
  speaking (no human voice).
"""

TextPipeline = Literal["text_analyzer", "passthrough"]
"""How a typed text-channel utterance becomes a ``SpeechEvent``:

* ``text_analyzer`` — one ``TextAnalyzer`` call per typed message
  extracts ``addressed_name`` / ``co_claim`` / ``role_callout`` so
  ``SpeakArbiter`` can route NPC responses to the addressed seat.
* ``passthrough`` — typed messages produce ``SpeechEvent`` rows with
  all structured fields ``None``. ``SpeakArbiter`` falls back to LRU
  rotation only.
"""

AudioAnalyzerProvider = Literal["gemini"]
"""Backends that accept audio input and return transcript + structured
analysis in one hop. Only Gemini Flash for now (xAI / Groq don't expose
audio-in chat-completion endpoints)."""

SttProvider = Literal["groq"]
"""Backends that transcribe audio to text only. Groq Whisper for now."""

TextAnalyzerProvider = Literal["xai", "deepseek", "openai", "gemini"]
"""Backends that accept text input and return structured analysis JSON.
``xai``/``deepseek``/``openai`` use the shared OpenAI Chat Completions
schema; ``gemini`` uses the AI Studio REST API."""


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

    # ── Voice / Text ingest pipeline shapes ────────────────────────────
    # Two orthogonal switches define the structural shape of human-input
    # ingestion. Each pipeline is composed from independent components
    # (AudioAnalyzer / Stt / TextAnalyzer) configured below. Defaults are
    # ``disabled``/``passthrough`` so a fresh checkout boots without
    # touching VC or burning analyzer tokens; reactive_voice operators
    # opt in explicitly.
    VOICE_PIPELINE: VoicePipeline = "disabled"
    TEXT_PIPELINE: TextPipeline = "passthrough"

    # ── AudioAnalyzer (audio → transcript + structured) ───────────────
    # Used only when ``VOICE_PIPELINE=audio_analyzer``.  Single multimodal
    # call: cheap and lowest-latency, but each game segment shares the
    # same RPM bucket as any text-channel call that piggy-backs on the
    # same key — keep the credential isolated unless you intentionally
    # want shared throttling.
    AUDIO_ANALYZER_PROVIDER: AudioAnalyzerProvider = "gemini"
    AUDIO_ANALYZER_API_KEY: SecretStr | None = None
    AUDIO_ANALYZER_MODEL: str = "gemini-2.0-flash-lite"

    # ── Stt (audio → transcript) ──────────────────────────────────────
    # Used only when ``VOICE_PIPELINE=stt_then_text_analyzer``.  Pairs
    # with the ``TEXT_ANALYZER_*`` block to form a two-step voice path.
    # ``whisper-large-v3-turbo`` is the cheapest multilingual Whisper
    # variant on Groq that still handles Japanese well; switch to
    # ``whisper-large-v3`` for max accuracy at ~3x the cost.
    STT_PROVIDER: SttProvider = "groq"
    STT_API_KEY: SecretStr | None = None
    STT_MODEL: str = "whisper-large-v3-turbo"
    STT_BASE_URL: str = "https://api.groq.com/openai/v1"

    # ── TextAnalyzer (text → structured) ──────────────────────────────
    # Used by ``TEXT_PIPELINE=text_analyzer`` (per typed message) AND by
    # ``VOICE_PIPELINE=stt_then_text_analyzer`` (per voice segment, after
    # STT). Same component, single credential pool, deliberately
    # decoupled from ``GAMEPLAY_LLM_*`` so Gameplay can target a
    # heavyweight reasoning model while text analysis hits a cheap one.
    #
    # Provider gating:
    #   * ``xai`` / ``deepseek`` / ``openai`` use the shared OpenAI Chat
    #     Completions schema and require ``TEXT_ANALYZER_API_KEY``.
    #   * ``gemini`` uses the AI Studio REST API and requires
    #     ``TEXT_ANALYZER_API_KEY`` (an AIza-format key). Vertex auth is
    #     not supported on this path because the analyzer uses the
    #     ``v1beta/models/{m}:generateContent?key=...`` shape.
    TEXT_ANALYZER_PROVIDER: TextAnalyzerProvider = "xai"
    TEXT_ANALYZER_API_KEY: SecretStr | None = None
    TEXT_ANALYZER_MODEL: str = "grok-4-1-fast"
    # Optional override for OpenAI-compatible endpoints (vLLM, Ollama,
    # etc.). Empty / None → provider default base URL.
    TEXT_ANALYZER_BASE_URL: str | None = None

    # ── Pre-STT silence gate ──────────────────────────────────────────
    # Discord's speaking-start fires on any audio above a low threshold
    # (breathing, keyboard, room hum). With ``SilenceGeneratorSink``
    # padding, this would burn one Stt + one TextAnalyzer call for
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
    def _require_pipeline_components(self) -> MasterSettings:
        """Enforce credential presence given the chosen pipeline shape.

        Each component check fires only when the pipeline actually
        instantiates that component. Operators with
        ``VOICE_PIPELINE=disabled`` and ``TEXT_PIPELINE=passthrough``
        boot without any analyzer credential at all (rounds-mode
        default).
        """
        # AudioAnalyzer is required only by the multimodal voice path.
        if (
            self.VOICE_PIPELINE == "audio_analyzer"
            and self.AUDIO_ANALYZER_API_KEY is None
        ):
            raise ValueError(
                "VOICE_PIPELINE=audio_analyzer requires "
                "AUDIO_ANALYZER_API_KEY to be set"
            )

        # Stt is required only by the split voice path.
        if (
            self.VOICE_PIPELINE == "stt_then_text_analyzer"
            and self.STT_API_KEY is None
        ):
            raise ValueError(
                "VOICE_PIPELINE=stt_then_text_analyzer requires "
                "STT_API_KEY to be set"
            )

        # TextAnalyzer is required by EITHER the text path OR the split
        # voice path (which composes Stt + TextAnalyzer).
        text_analyzer_required = (
            self.TEXT_PIPELINE == "text_analyzer"
            or self.VOICE_PIPELINE == "stt_then_text_analyzer"
        )
        if text_analyzer_required and self.TEXT_ANALYZER_API_KEY is None:
            raise ValueError(
                "TEXT_ANALYZER_API_KEY must be set when "
                "TEXT_PIPELINE=text_analyzer or "
                "VOICE_PIPELINE=stt_then_text_analyzer"
            )

        # The split voice path's analyzer step calls an OpenAI-compatible
        # /chat/completions endpoint (see GroqWhisperAudioAnalyzer). The
        # AI Studio REST shape used by the gemini text analyzer doesn't
        # fit, so block the combo at boot rather than failing per
        # segment.
        if (
            self.VOICE_PIPELINE == "stt_then_text_analyzer"
            and self.TEXT_ANALYZER_PROVIDER == "gemini"
        ):
            raise ValueError(
                "VOICE_PIPELINE=stt_then_text_analyzer composes Stt with "
                "an OpenAI-compatible TextAnalyzer; TEXT_ANALYZER_PROVIDER="
                "gemini is not supported on that path. Either set "
                "TEXT_ANALYZER_PROVIDER to xai/deepseek/openai or switch "
                "VOICE_PIPELINE to audio_analyzer."
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
