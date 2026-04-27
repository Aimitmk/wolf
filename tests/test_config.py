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
