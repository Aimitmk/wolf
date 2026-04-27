"""NPC settings provider-switch tests.

The NPC namespace mirrors GAMEPLAY_LLM_*; this file asserts the same
validator semantics under the NPC_LLM_* prefix.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from wolfbot.npc.config import NpcSettings


def _base_kwargs() -> dict[str, object]:
    return {
        "NPC_ID": "npc_setsu",
        "NPC_DISCORD_TOKEN": SecretStr("t"),
        "DISCORD_GUILD_ID": 1,
        "MAIN_VOICE_CHANNEL_ID": 2,
        "NPC_PERSONA_KEY": "setsu",
        "MASTER_WS_URL": "ws://127.0.0.1:8800",
        "MASTER_NPC_PSK": SecretStr("psk"),
    }


def test_default_provider_is_xai_and_requires_api_key() -> None:
    with pytest.raises(ValidationError):
        NpcSettings(_env_file=None, **_base_kwargs())  # type: ignore[arg-type]


def test_xai_provider_with_key_constructs() -> None:
    s = NpcSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        NPC_LLM_API_KEY=SecretStr("k"),
    )
    assert s.NPC_LLM_PROVIDER == "xai"
    assert s.NPC_LLM_MODEL == "grok-4-1-fast"


def test_deepseek_provider_requires_api_key() -> None:
    with pytest.raises(ValidationError):
        NpcSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_base_kwargs(),
            NPC_LLM_PROVIDER="deepseek",
        )


def test_gemini_provider_requires_vertex_project() -> None:
    with pytest.raises(ValidationError):
        NpcSettings(  # type: ignore[arg-type]
            _env_file=None,
            **_base_kwargs(),
            NPC_LLM_PROVIDER="gemini",
        )


def test_gemini_provider_does_not_require_api_key() -> None:
    s = NpcSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        NPC_LLM_PROVIDER="gemini",
        NPC_LLM_VERTEX_PROJECT="proj",
    )
    assert s.NPC_LLM_API_KEY is None
    assert s.NPC_LLM_VERTEX_PROJECT == "proj"


def test_npc_decider_config_round_trips_deepseek() -> None:
    s = NpcSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        NPC_LLM_PROVIDER="deepseek",
        NPC_LLM_API_KEY=SecretStr("d"),
    )
    cfg = s.npc_decider_config()
    assert cfg.provider == "deepseek"
    assert cfg.api_key is not None
    assert cfg.api_key.get_secret_value() == "d"
    assert cfg.thinking == "enabled"
    assert cfg.reasoning_effort == "max"


def test_mock_provider_does_not_require_api_key_or_vertex_project() -> None:
    """``NPC_LLM_PROVIDER=mock`` is for offline integration tests —
    no credentials should be required."""
    s = NpcSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        NPC_LLM_PROVIDER="mock",
    )
    assert s.NPC_LLM_PROVIDER == "mock"
    assert s.NPC_LLM_API_KEY is None
    assert s.NPC_LLM_VERTEX_PROJECT is None


def test_npc_decider_config_round_trips_mock() -> None:
    s = NpcSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        NPC_LLM_PROVIDER="mock",
    )
    cfg = s.npc_decider_config()
    assert cfg.provider == "mock"
    assert cfg.api_key is None
