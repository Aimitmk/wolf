"""Tests for the env-driven Settings models.

We pass `_env_file=None` everywhere so the repo's actual `.env.master`
(which has real values) cannot leak into and mask validator failures we
want to assert on.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from wolfbot.config import MasterSettings


def _base_kwargs() -> dict[str, object]:
    return {
        "DISCORD_TOKEN": SecretStr("token"),
        "DISCORD_GUILD_ID": 1,
        "MAIN_TEXT_CHANNEL_ID": 2,
        "MAIN_VOICE_CHANNEL_ID": 3,
    }


# ----------------------------------------------------------- xAI provider
def test_default_provider_is_xai_and_requires_gameplay_api_key() -> None:
    with pytest.raises(ValidationError):
        MasterSettings(_env_file=None, **_base_kwargs())  # type: ignore[arg-type]


def test_xai_provider_with_key_constructs() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        GAMEPLAY_LLM_API_KEY=SecretStr("k"),
    )
    assert s.GAMEPLAY_LLM_PROVIDER == "xai"
    assert s.GAMEPLAY_LLM_MODEL == "grok-4-1-fast"
    assert s.GAMEPLAY_LLM_BASE_URL is None
    # DeepSeek defaults still readable but unused on the xai path.
    assert s.GAMEPLAY_LLM_THINKING == "enabled"
    assert s.GAMEPLAY_LLM_REASONING_EFFORT == "max"


def test_xai_provider_can_override_base_url() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        GAMEPLAY_LLM_API_KEY=SecretStr("k"),
        GAMEPLAY_LLM_BASE_URL="http://localhost:11434/v1",
    )
    assert s.GAMEPLAY_LLM_BASE_URL == "http://localhost:11434/v1"


# ------------------------------------------------------ DeepSeek provider
def test_deepseek_provider_requires_api_key() -> None:
    with pytest.raises(ValidationError):
        MasterSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_base_kwargs(),
            GAMEPLAY_LLM_PROVIDER="deepseek",
        )


def test_deepseek_provider_with_key_constructs() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        GAMEPLAY_LLM_PROVIDER="deepseek",
        GAMEPLAY_LLM_API_KEY=SecretStr("d"),
    )
    assert s.GAMEPLAY_LLM_PROVIDER == "deepseek"
    assert s.GAMEPLAY_LLM_THINKING == "enabled"
    assert s.GAMEPLAY_LLM_REASONING_EFFORT == "max"


def test_thinking_literal_is_strict() -> None:
    with pytest.raises(ValidationError):
        MasterSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_base_kwargs(),
            GAMEPLAY_LLM_PROVIDER="deepseek",
            GAMEPLAY_LLM_API_KEY=SecretStr("d"),
            GAMEPLAY_LLM_THINKING="off",
        )


def test_reasoning_effort_literal_is_strict() -> None:
    with pytest.raises(ValidationError):
        MasterSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_base_kwargs(),
            GAMEPLAY_LLM_PROVIDER="deepseek",
            GAMEPLAY_LLM_API_KEY=SecretStr("d"),
            GAMEPLAY_LLM_REASONING_EFFORT="medium",
        )


# -------------------------------------------------------- Gemini provider
def test_gemini_provider_requires_vertex_project() -> None:
    with pytest.raises(ValidationError):
        MasterSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_base_kwargs(),
            GAMEPLAY_LLM_PROVIDER="gemini",
        )


def test_gemini_provider_does_not_require_api_key() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        GAMEPLAY_LLM_PROVIDER="gemini",
        GAMEPLAY_LLM_VERTEX_PROJECT="my-project",
    )
    assert s.GAMEPLAY_LLM_API_KEY is None
    assert s.GAMEPLAY_LLM_VERTEX_PROJECT == "my-project"
    assert s.GAMEPLAY_LLM_VERTEX_LOCATION == "global"
    assert s.GAMEPLAY_LLM_THINKING_LEVEL == "high"


def test_gemini_thinking_level_literal_is_strict() -> None:
    with pytest.raises(ValidationError):
        MasterSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_base_kwargs(),
            GAMEPLAY_LLM_PROVIDER="gemini",
            GAMEPLAY_LLM_VERTEX_PROJECT="p",
            GAMEPLAY_LLM_THINKING_LEVEL="off",
        )


def test_gemini_empty_vertex_project_rejected() -> None:
    """Empty string in .env should be rejected at boot, not deferred to
    the SDK at first request time."""
    with pytest.raises(ValidationError):
        MasterSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_base_kwargs(),
            GAMEPLAY_LLM_PROVIDER="gemini",
            GAMEPLAY_LLM_VERTEX_PROJECT="",
        )


# --------------------------------------------------------------- mock provider
def test_mock_provider_does_not_require_api_key_or_vertex_project() -> None:
    """``GAMEPLAY_LLM_PROVIDER=mock`` is for offline integration tests —
    no credentials should be required."""
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        GAMEPLAY_LLM_PROVIDER="mock",
    )
    assert s.GAMEPLAY_LLM_PROVIDER == "mock"
    assert s.GAMEPLAY_LLM_API_KEY is None
    assert s.GAMEPLAY_LLM_VERTEX_PROJECT is None


def test_mock_decider_config_round_trips() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        GAMEPLAY_LLM_PROVIDER="mock",
    )
    cfg = s.gameplay_decider_config()
    assert cfg.provider == "mock"
    assert cfg.api_key is None


# ---------------------------------------------------------------- common
def test_unknown_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        MasterSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_base_kwargs(),
            GAMEPLAY_LLM_PROVIDER="claude",
        )


def test_gameplay_decider_config_round_trips_xai() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        GAMEPLAY_LLM_API_KEY=SecretStr("k"),
    )
    cfg = s.gameplay_decider_config()
    assert cfg.provider == "xai"
    assert cfg.api_key is not None
    assert cfg.api_key.get_secret_value() == "k"
    assert cfg.model == "grok-4-1-fast"


def test_gameplay_decider_config_round_trips_gemini() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        GAMEPLAY_LLM_PROVIDER="gemini",
        GAMEPLAY_LLM_VERTEX_PROJECT="p",
    )
    cfg = s.gameplay_decider_config()
    assert cfg.provider == "gemini"
    assert cfg.vertex_project == "p"
    assert cfg.vertex_location == "global"
    assert cfg.thinking_level == "high"


# ──────────────────────────────────────────────── ingest pipelines
def _gameplay_ok_kwargs() -> dict[str, object]:
    """Minimal valid gameplay LLM kwargs so pipeline tests can focus on
    the new VOICE_/TEXT_ pipeline validators without fighting the
    gameplay validator."""
    return {**_base_kwargs(), "GAMEPLAY_LLM_API_KEY": SecretStr("g")}


def test_pipeline_defaults_require_no_ingest_credentials() -> None:
    """Rounds-mode boot: nothing voice/text-related needed. The
    defaults (``disabled``/``passthrough``) keep validators silent so
    ops with no analyzer key at all can still launch the bot."""
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_gameplay_ok_kwargs(),
    )
    assert s.VOICE_PIPELINE == "disabled"
    assert s.TEXT_PIPELINE == "passthrough"
    assert s.AUDIO_ANALYZER_API_KEY is None
    assert s.STT_API_KEY is None
    assert s.TEXT_ANALYZER_API_KEY is None


def test_voice_pipeline_audio_analyzer_requires_audio_key() -> None:
    with pytest.raises(ValidationError, match="AUDIO_ANALYZER_API_KEY"):
        MasterSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_gameplay_ok_kwargs(),
            VOICE_PIPELINE="audio_analyzer",
        )


def test_voice_pipeline_audio_analyzer_with_key_constructs() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_gameplay_ok_kwargs(),
        VOICE_PIPELINE="audio_analyzer",
        AUDIO_ANALYZER_API_KEY=SecretStr("aa"),
    )
    assert s.VOICE_PIPELINE == "audio_analyzer"
    assert s.AUDIO_ANALYZER_PROVIDER == "gemini"


def test_voice_pipeline_split_requires_stt_and_text_analyzer_keys() -> None:
    """``stt_then_text_analyzer`` composes Stt + TextAnalyzer; both
    credentials must be present."""
    # Missing STT key.
    with pytest.raises(ValidationError, match="STT_API_KEY"):
        MasterSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_gameplay_ok_kwargs(),
            VOICE_PIPELINE="stt_then_text_analyzer",
            TEXT_ANALYZER_API_KEY=SecretStr("ta"),
        )
    # Missing TextAnalyzer key.
    with pytest.raises(ValidationError, match="TEXT_ANALYZER_API_KEY"):
        MasterSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_gameplay_ok_kwargs(),
            VOICE_PIPELINE="stt_then_text_analyzer",
            STT_API_KEY=SecretStr("ss"),
        )


def test_voice_pipeline_split_rejects_gemini_text_analyzer() -> None:
    """Cross-field guard: Whisper-then-Gemini composition isn't wired up
    in the runtime so the validator must fail loud at boot rather than
    deferring to a runtime AttributeError."""
    with pytest.raises(ValidationError, match="not supported"):
        MasterSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_gameplay_ok_kwargs(),
            VOICE_PIPELINE="stt_then_text_analyzer",
            STT_API_KEY=SecretStr("ss"),
            TEXT_ANALYZER_PROVIDER="gemini",
            TEXT_ANALYZER_API_KEY=SecretStr("ta"),
        )


def test_voice_pipeline_split_constructs_with_xai_text_analyzer() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_gameplay_ok_kwargs(),
        VOICE_PIPELINE="stt_then_text_analyzer",
        STT_API_KEY=SecretStr("ss"),
        TEXT_ANALYZER_API_KEY=SecretStr("ta"),
    )
    assert s.VOICE_PIPELINE == "stt_then_text_analyzer"
    assert s.STT_PROVIDER == "groq"
    assert s.TEXT_ANALYZER_PROVIDER == "xai"


def test_text_pipeline_text_analyzer_requires_key() -> None:
    """Text path alone needs TextAnalyzer credentials even when voice
    is disabled — they don't piggy-back on each other anymore."""
    with pytest.raises(ValidationError, match="TEXT_ANALYZER_API_KEY"):
        MasterSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_gameplay_ok_kwargs(),
            TEXT_PIPELINE="text_analyzer",
        )


def test_text_pipeline_text_analyzer_with_key_constructs() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_gameplay_ok_kwargs(),
        TEXT_PIPELINE="text_analyzer",
        TEXT_ANALYZER_API_KEY=SecretStr("ta"),
    )
    assert s.TEXT_PIPELINE == "text_analyzer"
    assert s.TEXT_ANALYZER_MODEL == "grok-4-1-fast"


def test_audio_voice_text_pipelines_are_independent() -> None:
    """Bug fix coverage: configuring the voice path with Gemini must not
    force the text path through the same credential. Set the typical
    ``Gemini multimodal voice + xAI text analyzer`` combo and assert
    both knobs land where intended."""
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_gameplay_ok_kwargs(),
        VOICE_PIPELINE="audio_analyzer",
        AUDIO_ANALYZER_API_KEY=SecretStr("gemini-key"),
        TEXT_PIPELINE="text_analyzer",
        TEXT_ANALYZER_PROVIDER="xai",
        TEXT_ANALYZER_API_KEY=SecretStr("xai-key"),
    )
    assert s.AUDIO_ANALYZER_PROVIDER == "gemini"
    assert s.AUDIO_ANALYZER_API_KEY is not None
    assert s.AUDIO_ANALYZER_API_KEY.get_secret_value() == "gemini-key"
    assert s.TEXT_ANALYZER_PROVIDER == "xai"
    assert s.TEXT_ANALYZER_API_KEY is not None
    assert s.TEXT_ANALYZER_API_KEY.get_secret_value() == "xai-key"
