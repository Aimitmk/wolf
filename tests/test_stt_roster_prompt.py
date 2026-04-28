"""Roster grounding for the STT analyzer prompt.

The analyzer LLM receives a list of ``(seat_no, display_name)`` pairs
so it can resolve mistranscribed names (Whisper's "ラッキーオ" for
spoken "ラキオ" is the canonical failure case) to the canonical
participant. These tests pin the prompt-construction contract:

1. ``_format_roster_block`` returns an empty string for missing /
   empty roster so callers without seat info get the legacy prompt.
2. A non-empty roster is rendered as numbered lines with the
   variant-tolerance instructions the analyzer needs.
3. Both ``GeminiAudioAnalyzer._build_system_prompt`` and
   ``GroqWhisperAudioAnalyzer._build_analyzer_prompt`` thread the
   roster through correctly.
"""

from __future__ import annotations

from wolfbot.master.stt_service import (
    GeminiAudioAnalyzer,
    GroqWhisperAudioAnalyzer,
    _format_roster_block,
)


def test_format_roster_block_empty_when_roster_missing() -> None:
    assert _format_roster_block(None) == ""
    assert _format_roster_block([]) == ""


def test_format_roster_block_lists_seats_and_variant_rules() -> None:
    block = _format_roster_block([(1, "🦋ラキオ"), (2, "🌙セツ")])
    assert "席1: 🦋ラキオ" in block
    assert "席2: 🌙セツ" in block
    # The crucial instruction: the LLM must collapse phonetic /
    # mistranscribed variants onto a roster entry, never invent a
    # new one. The example call-out is the canonical failure case
    # from the voicetest dump that motivated this feature.
    assert "ラッキーオ" in block
    assert "敬称" in block
    # Catch-all behaviors: out-of-roster / ambiguous → null.
    assert "null" in block


def test_gemini_build_system_prompt_appends_roster_block() -> None:
    base = GeminiAudioAnalyzer._SYSTEM_PROMPT_BASE
    with_roster = GeminiAudioAnalyzer._build_system_prompt(
        [(3, "🦋ラキオ")]
    )
    assert with_roster.startswith(base)
    assert "席3: 🦋ラキオ" in with_roster


def test_gemini_build_system_prompt_no_roster_equals_base() -> None:
    assert (
        GeminiAudioAnalyzer._build_system_prompt(None)
        == GeminiAudioAnalyzer._SYSTEM_PROMPT_BASE
    )


def test_groq_build_analyzer_prompt_appends_roster_block() -> None:
    base = GroqWhisperAudioAnalyzer._ANALYZER_PROMPT_BASE
    with_roster = GroqWhisperAudioAnalyzer._build_analyzer_prompt(
        [(5, "🌙セツ")]
    )
    assert with_roster.startswith(base)
    assert "席5: 🌙セツ" in with_roster


def test_groq_build_analyzer_prompt_no_roster_equals_base() -> None:
    assert (
        GroqWhisperAudioAnalyzer._build_analyzer_prompt(None)
        == GroqWhisperAudioAnalyzer._ANALYZER_PROMPT_BASE
    )
