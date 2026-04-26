"""Tests for the env-driven Settings model.

We pass `_env_file=None` everywhere so the repo's actual `.env` (which has
real values for `DISCORD_TOKEN`, `XAI_API_KEY`, etc.) cannot leak into and
mask validator failures we want to assert on.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from wolfbot.config import Settings


def _base_kwargs() -> dict[str, object]:
    return {
        "DISCORD_TOKEN": SecretStr("token"),
        "DISCORD_GUILD_ID": 1,
        "MAIN_TEXT_CHANNEL_ID": 2,
        "MAIN_VOICE_CHANNEL_ID": 3,
    }


def test_default_provider_is_xai_and_requires_xai_key() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_base_kwargs())  # type: ignore[arg-type]


def test_xai_provider_with_key_constructs() -> None:
    s = Settings(_env_file=None, **_base_kwargs(), XAI_API_KEY=SecretStr("k"))  # type: ignore[arg-type]
    assert s.LLM_PROVIDER == "xai"
    assert s.XAI_MODEL == "grok-4-1-fast"
    # DeepSeek defaults still readable but unused on the xai path.
    assert s.DEEPSEEK_BASE_URL == "https://api.deepseek.com"
    assert s.DEEPSEEK_MODEL == "deepseek-v4-flash"


def test_deepseek_provider_requires_deepseek_key() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_base_kwargs(), LLM_PROVIDER="deepseek")  # type: ignore[arg-type]


def test_deepseek_provider_does_not_require_xai_key() -> None:
    s = Settings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        LLM_PROVIDER="deepseek",
        DEEPSEEK_API_KEY=SecretStr("d"),
    )
    assert s.XAI_API_KEY is None
    assert s.DEEPSEEK_THINKING == "enabled"
    assert s.DEEPSEEK_REASONING_EFFORT == "max"
    assert s.DEEPSEEK_MODEL == "deepseek-v4-flash"


def test_unknown_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_base_kwargs(), LLM_PROVIDER="claude")  # type: ignore[arg-type]


def test_thinking_literal_is_strict() -> None:
    base = {**_base_kwargs(), "LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": SecretStr("d")}
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **base, DEEPSEEK_THINKING="off")  # type: ignore[arg-type]


def test_reasoning_effort_literal_is_strict() -> None:
    base = {**_base_kwargs(), "LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": SecretStr("d")}
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **base, DEEPSEEK_REASONING_EFFORT="medium")  # type: ignore[arg-type]


def test_gemini_provider_requires_gemini_vertex_project() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_base_kwargs(), LLM_PROVIDER="gemini")  # type: ignore[arg-type]


def test_gemini_provider_does_not_require_xai_or_deepseek_key() -> None:
    s = Settings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        LLM_PROVIDER="gemini",
        GEMINI_VERTEX_PROJECT="my-project",
    )
    assert s.XAI_API_KEY is None
    assert s.DEEPSEEK_API_KEY is None
    assert s.GEMINI_VERTEX_PROJECT == "my-project"
    assert s.GEMINI_VERTEX_LOCATION == "global"
    assert s.GEMINI_MODEL == "gemini-3-flash-preview"
    assert s.GEMINI_THINKING_LEVEL == "high"


def test_gemini_thinking_level_literal_is_strict() -> None:
    base = {
        **_base_kwargs(),
        "LLM_PROVIDER": "gemini",
        "GEMINI_VERTEX_PROJECT": "my-project",
    }
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **base, GEMINI_THINKING_LEVEL="off")  # type: ignore[arg-type]


def test_gemini_api_key_alone_without_vertex_project_rejected() -> None:
    """Stale GEMINI_API_KEY in env is silently dropped by extra='ignore';
    the missing GEMINI_VERTEX_PROJECT still raises a ValidationError."""
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[arg-type, call-arg]
            _env_file=None,
            **_base_kwargs(),
            LLM_PROVIDER="gemini",
            GEMINI_API_KEY=SecretStr("g"),
        )


def test_gemini_vertex_location_default_is_global() -> None:
    s = Settings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        LLM_PROVIDER="gemini",
        GEMINI_VERTEX_PROJECT="p",
    )
    assert s.GEMINI_VERTEX_LOCATION == "global"


def test_gemini_empty_vertex_project_rejected() -> None:
    """Empty string in .env should be rejected at boot, not deferred to
    the SDK at first request time."""
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[arg-type]
            _env_file=None,
            **_base_kwargs(),
            LLM_PROVIDER="gemini",
            GEMINI_VERTEX_PROJECT="",
        )
