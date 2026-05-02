"""GeminiTextAnalyzer + FakeTextAnalyzer — parser and stub coverage.

The analyzer mirrors `GeminiAudioAnalyzer` for the text channel. End-to-end
routing (analyzer → SpeechEvent.addressed_seat_no → SpeakArbiter dispatch)
is covered by `test_addressed_npc_routing.py`; this file exercises the
analyzer module in isolation.
"""

from __future__ import annotations

import pytest

from wolfbot.master.state.text_analyzer import (
    FakeTextAnalyzer,
    GeminiTextAnalyzer,
    TextAnalysis,
)


def test_parse_response_handles_plain_json() -> None:
    raw = '{"co_claim":"seer","addressed_name":"セツ"}'
    parsed = GeminiTextAnalyzer._parse_response(raw)
    assert parsed == {"co_claim": "seer", "addressed_name": "セツ"}


def test_parse_response_strips_markdown_fences() -> None:
    raw = '```json\n{"co_claim":null,"addressed_name":"席3"}\n```'
    parsed = GeminiTextAnalyzer._parse_response(raw)
    assert parsed == {"co_claim": None, "addressed_name": "席3"}


def test_parse_response_returns_empty_on_garbage() -> None:
    parsed = GeminiTextAnalyzer._parse_response("not-json")
    assert parsed == {}


async def test_fake_analyzer_returns_scripted_in_order() -> None:
    fake = FakeTextAnalyzer(
        scripted=[
            TextAnalysis(addressed_name="セツ"),
            TextAnalysis(co_declaration="seer"),
        ]
    )
    a = await fake.analyze(text="セツさん、どう？", timeout_s=5.0)
    b = await fake.analyze(text="占いCO", timeout_s=5.0)
    assert a.addressed_name == "セツ"
    assert b.co_declaration == "seer"
    assert fake.call_count == 2


async def test_fake_analyzer_falls_through_to_default() -> None:
    fake = FakeTextAnalyzer(default=TextAnalysis())
    a = await fake.analyze(text="特になし", timeout_s=5.0)
    assert a.addressed_name is None and a.co_declaration is None


async def test_fake_analyzer_records_last_text_for_assertions() -> None:
    fake = FakeTextAnalyzer(default=TextAnalysis())
    await fake.analyze(text="記録される", timeout_s=5.0)
    assert fake.last_text == "記録される"


async def test_openai_compatible_text_analyzer_extracts_role_callout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When voice ingest uses Groq+xAI (separated STT + analyzer), the
    text path must follow the same split — sending typed messages to the
    same OpenAI-compatible analyzer endpoint instead of round-tripping
    through Gemini. Without this, Gemini rate limits silently kill the
    role_callout signal so wolf-side NPCs never see "占い師の方どうぞ"
    requests (regression: game 58a3243a9fb8).
    """
    import json as _json
    from typing import Any

    import httpx

    from wolfbot.master.state.text_analyzer import OpenAICompatibleTextAnalyzer

    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = _json.loads(request.content.decode("utf-8"))
        payload = {
            "choices": [
                {
                    "message": {
                        "content": _json.dumps(
                            {
                                "co_claim": None,
                                "addressed_name": None,
                                "role_callout": "seer",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 70,
                "completion_tokens": 10,
                "total_tokens": 80,
            },
        }
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient

    class _MockedClient(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("transport", transport)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", _MockedClient)

    analyzer = OpenAICompatibleTextAnalyzer(
        api_key="sk-test",
        model="grok-test",
        base_url="https://api.x.ai/v1",
    )
    analysis = await analyzer.analyze(
        text="本当の占い師出てきて、人狼さいどでもいい",
        timeout_s=5.0,
    )

    assert analysis.role_callout == "seer"
    assert analysis.co_declaration is None
    assert analysis.addressed_name is None
    # The endpoint and auth shape match the OpenAI Chat Completions
    # contract used by GroqWhisperAudioAnalyzer's analyzer step.
    assert captured["url"] == "https://api.x.ai/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer sk-test"
    assert captured["body"]["model"] == "grok-test"
    assert captured["body"]["response_format"] == {"type": "json_object"}


async def test_fake_analyzer_propagates_scripted_exception() -> None:
    """A scripted exception must be raised verbatim so the cog's broad
    try/except logs and falls back to raw capture."""

    class _Boom(RuntimeError):
        pass

    fake = FakeTextAnalyzer(scripted=[_Boom("kaboom")])
    with pytest.raises(_Boom):
        await fake.analyze(text="x", timeout_s=1.0)


async def test_fake_analyzer_no_default_returns_empty_analysis_when_unscripted() -> None:
    """When no scripted result and no default is set, FakeTextAnalyzer must
    return an empty TextAnalysis (matches a Gemini call that found nothing)."""
    fake = FakeTextAnalyzer()
    result = await fake.analyze(text="anything", timeout_s=1.0)
    assert result == TextAnalysis()
