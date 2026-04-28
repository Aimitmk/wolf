"""Tests for `wolfbot.services.llm_trace` JSONL writer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wolfbot.services.llm_trace import (
    log_llm_call,
    parse_day_from_phase_id,
    parse_game_id_from_phase_id,
    trace_context,
)


@pytest.fixture
def trace_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WOLFBOT_LLM_TRACE_DIR", str(tmp_path))
    monkeypatch.delenv("WOLFBOT_LLM_TRACE_DISABLED", raising=False)
    return tmp_path


async def test_log_llm_call_writes_jsonl_line(trace_dir: Path) -> None:
    with trace_context(
        game_id="g_abc",
        phase="DAY_DISCUSSION",
        day=2,
        actor="seat=4 persona=setsu",
        metadata={"task": "vote"},
    ):
        await log_llm_call(
            role="gameplay",
            provider="xai",
            model="grok-4-1-fast",
            system_prompt="sys",
            user_prompt="usr",
            response='{"intent":"vote"}',
            latency_ms=123,
        )

    path = trace_dir / "g_abc" / "gameplay.jsonl"
    assert path.exists(), "trace file was not created"
    line = path.read_text(encoding="utf-8").strip()
    entry = json.loads(line)

    assert entry["role"] == "gameplay"
    assert entry["provider"] == "xai"
    assert entry["model"] == "grok-4-1-fast"
    assert entry["phase"] == "DAY_DISCUSSION"
    assert entry["day"] == 2
    assert entry["actor"] == "seat=4 persona=setsu"
    assert entry["system_prompt"] == "sys"
    assert entry["user_prompt"] == "usr"
    assert entry["response"] == '{"intent":"vote"}'
    assert entry["latency_ms"] == 123
    assert entry["error"] is None
    assert entry["metadata"] == {"task": "vote"}
    assert entry["ts"].endswith("+00:00")


async def test_log_llm_call_appends_multiple_lines(trace_dir: Path) -> None:
    with trace_context(game_id="g_one"):
        for i in range(3):
            await log_llm_call(
                role="gameplay",
                provider="gemini",
                model="gemini-3-flash",
                system_prompt="s",
                user_prompt=f"u{i}",
                response="r",
                latency_ms=i,
            )

    path = trace_dir / "g_one" / "gameplay.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    payloads = [json.loads(line) for line in lines]
    assert [p["user_prompt"] for p in payloads] == ["u0", "u1", "u2"]


async def test_log_llm_call_uses_no_game_when_context_missing(trace_dir: Path) -> None:
    await log_llm_call(
        role="voice_stt",
        provider="gemini",
        model="gemini-2.0-flash-lite",
        system_prompt="sys",
        user_prompt="[audio]",
        response='{"transcript":"hi"}',
        latency_ms=42,
    )
    path = trace_dir / "no_game" / "voice_stt.jsonl"
    assert path.exists()


async def test_log_llm_call_respects_file_stem_override(trace_dir: Path) -> None:
    with trace_context(game_id="g_two"):
        await log_llm_call(
            role="npc_speech",
            provider="openai-compat",
            model="grok-4-1-fast",
            system_prompt="s",
            user_prompt="u",
            response="r",
            latency_ms=10,
            file_stem="npc_setsu",
        )
    assert (trace_dir / "g_two" / "npc_setsu.jsonl").exists()


async def test_log_llm_call_records_error_path(trace_dir: Path) -> None:
    with trace_context(game_id="g_err"):
        await log_llm_call(
            role="gameplay",
            provider="xai",
            model="grok-4-1-fast",
            system_prompt="s",
            user_prompt="u",
            response=None,
            latency_ms=5000,
            error="TimeoutError: no response",
        )
    entry = json.loads(
        (trace_dir / "g_err" / "gameplay.jsonl").read_text(encoding="utf-8").strip()
    )
    assert entry["response"] is None
    assert entry["error"] == "TimeoutError: no response"


async def test_disabled_via_env_writes_nothing(
    trace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WOLFBOT_LLM_TRACE_DISABLED", "1")
    with trace_context(game_id="g_disabled"):
        await log_llm_call(
            role="gameplay",
            provider="xai",
            model="grok-4-1-fast",
            system_prompt="s",
            user_prompt="u",
            response="r",
            latency_ms=1,
        )
    assert not (trace_dir / "g_disabled").exists()


async def test_path_components_are_sanitized(trace_dir: Path) -> None:
    # game_id with path-traversal-ish chars
    with trace_context(game_id="../evil/../escape"):
        await log_llm_call(
            role="gameplay",
            provider="xai",
            model="grok-4-1-fast",
            system_prompt="s",
            user_prompt="u",
            response="r",
            latency_ms=1,
        )
    # No directory should be created outside trace_dir.
    assert not (trace_dir.parent / "evil").exists()
    # The sanitized name lives inside trace_dir.
    children = [p.name for p in trace_dir.iterdir() if p.is_dir()]
    assert children, "expected a sanitized subdirectory"
    assert all(".." not in name for name in children)


async def test_extra_metadata_merges_with_context(trace_dir: Path) -> None:
    with trace_context(game_id="g_meta", metadata={"task": "vote"}):
        await log_llm_call(
            role="gameplay",
            provider="deepseek",
            model="deepseek-chat",
            system_prompt="s",
            user_prompt="u",
            response="r",
            latency_ms=1,
            extra={"thinking": "enabled", "reasoning_effort": "max"},
        )
    entry = json.loads(
        (trace_dir / "g_meta" / "gameplay.jsonl").read_text(encoding="utf-8").strip()
    )
    assert entry["metadata"] == {
        "task": "vote",
        "thinking": "enabled",
        "reasoning_effort": "max",
    }


async def test_log_llm_call_records_tokens(trace_dir: Path) -> None:
    with trace_context(game_id="g_tok"):
        await log_llm_call(
            role="gameplay",
            provider="xai",
            model="grok-4-1-fast",
            system_prompt="s",
            user_prompt="u",
            response="r",
            latency_ms=42,
            tokens={"prompt": 1500, "completion": 200, "total": 1700},
        )
    entry = json.loads(
        (trace_dir / "g_tok" / "gameplay.jsonl").read_text(encoding="utf-8").strip()
    )
    assert entry["tokens"] == {"prompt": 1500, "completion": 200, "total": 1700}


def test_extract_openai_tokens() -> None:
    from types import SimpleNamespace

    from wolfbot.services.llm_trace import extract_openai_tokens

    resp = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=1500, completion_tokens=200, total_tokens=1700
        )
    )
    assert extract_openai_tokens(resp) == {
        "prompt": 1500,
        "completion": 200,
        "total": 1700,
    }
    # Test fakes without `usage` must return None, never raise.
    assert extract_openai_tokens(SimpleNamespace()) is None


def test_extract_gemini_vertex_tokens() -> None:
    from types import SimpleNamespace

    from wolfbot.services.llm_trace import extract_gemini_vertex_tokens

    resp = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=2000,
            candidates_token_count=300,
            total_token_count=2300,
        )
    )
    assert extract_gemini_vertex_tokens(resp) == {
        "prompt": 2000,
        "completion": 300,
        "total": 2300,
    }
    assert extract_gemini_vertex_tokens(SimpleNamespace()) is None


def test_extract_gemini_rest_tokens() -> None:
    from wolfbot.services.llm_trace import extract_gemini_rest_tokens

    body = {
        "candidates": [],
        "usageMetadata": {
            "promptTokenCount": 800,
            "candidatesTokenCount": 100,
            "totalTokenCount": 900,
        },
    }
    assert extract_gemini_rest_tokens(body) == {
        "prompt": 800,
        "completion": 100,
        "total": 900,
    }
    assert extract_gemini_rest_tokens({}) is None


def test_parse_game_id_from_phase_id() -> None:
    assert parse_game_id_from_phase_id("g_abc::day1::DAY_DISCUSSION::1") == "g_abc"
    assert parse_game_id_from_phase_id(None) is None
    assert parse_game_id_from_phase_id("") is None
    assert parse_game_id_from_phase_id("noseparator") is None


def test_parse_day_from_phase_id() -> None:
    assert parse_day_from_phase_id("g_abc::day1::DAY_DISCUSSION::1") == 1
    assert parse_day_from_phase_id("g_abc::day12::NIGHT::3") == 12
    assert parse_day_from_phase_id("g_abc::day0::NIGHT_0::1") == 0
    assert parse_day_from_phase_id(None) is None
    assert parse_day_from_phase_id("") is None
    assert parse_day_from_phase_id("g_abc") is None
    assert parse_day_from_phase_id("g_abc::notaday::DAY_DISCUSSION::1") is None
    assert parse_day_from_phase_id("g_abc::dayX::DAY_DISCUSSION::1") is None


async def test_concurrent_writes_interleave_at_line_boundaries(
    trace_dir: Path,
) -> None:
    import asyncio

    async def write(i: int) -> None:
        with trace_context(game_id="g_concurrent"):
            await log_llm_call(
                role="gameplay",
                provider="xai",
                model="grok-4-1-fast",
                system_prompt="s",
                user_prompt=f"call_{i}",
                response="r",
                latency_ms=i,
            )

    await asyncio.gather(*(write(i) for i in range(20)))
    path = trace_dir / "g_concurrent" / "gameplay.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 20
    # Each line must parse as JSON — i.e. no torn writes.
    for line in lines:
        json.loads(line)
