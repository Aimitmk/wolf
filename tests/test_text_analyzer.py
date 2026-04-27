"""GeminiTextAnalyzer + FakeTextAnalyzer — parser and stub coverage.

The analyzer mirrors `GeminiAudioAnalyzer` for the text channel. End-to-end
routing (analyzer → SpeechEvent.addressed_seat_no → SpeakArbiter dispatch)
is covered by `test_addressed_npc_routing.py`; this file exercises the
analyzer module in isolation.
"""

from __future__ import annotations

import pytest

from wolfbot.master.text_analyzer import (
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
