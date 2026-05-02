"""Coverage for ``wolfbot.master.ingest_factory``.

The factory turns the structural pipeline switches on
:class:`MasterSettings` into ready-to-inject ``AudioAnalyzer`` / ``Stt``
/ ``TextAnalyzer`` instances. These tests pin two contracts:

1. **Returns ``None`` when the pipeline doesn't request the component.**
   Callers branch on the return value rather than re-checking pipeline
   shape; if a factory ever returns a stub instead of None, the wiring
   in ``main.py`` would silently activate a path the operator opted out
   of.
2. **Constructs the right concrete class for each combination.** The
   pipeline shape + provider together decide which of the four concrete
   analyzer/STT classes to instantiate. We don't exercise the network
   path here — those are covered in the per-class test files
   (``test_text_analyzer``, ``test_master_stt_groq``).
"""

from __future__ import annotations

from pydantic import SecretStr

from wolfbot.config import MasterSettings
from wolfbot.master.ingest_factory import (
    build_text_analyzer,
    build_voice_ingest_provider,
    voice_ingest_summary,
)
from wolfbot.master.state.text_analyzer import (
    GeminiTextAnalyzer,
    OpenAICompatibleTextAnalyzer,
)
from wolfbot.master.voice.stt_service import (
    GeminiAudioAnalyzer,
    GroqWhisperAudioAnalyzer,
)


def _base_kwargs() -> dict[str, object]:
    return {
        "DISCORD_TOKEN": SecretStr("token"),
        "DISCORD_GUILD_ID": 1,
        "MAIN_TEXT_CHANNEL_ID": 2,
        "MAIN_VOICE_CHANNEL_ID": 3,
        "GAMEPLAY_LLM_API_KEY": SecretStr("g"),
    }


# ───────────────────────────────────────────── build_text_analyzer
def test_text_analyzer_none_when_passthrough_and_voice_disabled() -> None:
    s = MasterSettings(_env_file=None, **_base_kwargs())  # type: ignore[arg-type]
    assert build_text_analyzer(s) is None


def test_text_analyzer_xai_default() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        TEXT_PIPELINE="text_analyzer",
        TEXT_ANALYZER_API_KEY=SecretStr("ta"),
    )
    inst = build_text_analyzer(s)
    assert isinstance(inst, OpenAICompatibleTextAnalyzer)
    # xai default base url applied even though TEXT_ANALYZER_BASE_URL is unset.
    assert inst.base_url == "https://api.x.ai/v1"
    assert inst.model == "grok-4-1-fast"


def test_text_analyzer_deepseek_default_base_url() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        TEXT_PIPELINE="text_analyzer",
        TEXT_ANALYZER_PROVIDER="deepseek",
        TEXT_ANALYZER_API_KEY=SecretStr("ta"),
    )
    inst = build_text_analyzer(s)
    assert isinstance(inst, OpenAICompatibleTextAnalyzer)
    assert inst.base_url == "https://api.deepseek.com"


def test_text_analyzer_explicit_base_url_wins() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        TEXT_PIPELINE="text_analyzer",
        TEXT_ANALYZER_API_KEY=SecretStr("ta"),
        TEXT_ANALYZER_BASE_URL="http://localhost:11434/v1",
    )
    inst = build_text_analyzer(s)
    assert isinstance(inst, OpenAICompatibleTextAnalyzer)
    assert inst.base_url == "http://localhost:11434/v1"


def test_text_analyzer_gemini_path() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        TEXT_PIPELINE="text_analyzer",
        TEXT_ANALYZER_PROVIDER="gemini",
        TEXT_ANALYZER_API_KEY=SecretStr("ta"),
        TEXT_ANALYZER_MODEL="gemini-2.0-flash-lite",
    )
    inst = build_text_analyzer(s)
    assert isinstance(inst, GeminiTextAnalyzer)
    assert inst.model == "gemini-2.0-flash-lite"


def test_text_analyzer_built_for_split_voice_path_even_without_text_pipeline() -> None:
    """``stt_then_text_analyzer`` voice path needs the analyzer too;
    factory must hand back the same kind of instance the text pipeline
    would have built."""
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        VOICE_PIPELINE="stt_then_text_analyzer",
        STT_API_KEY=SecretStr("ss"),
        TEXT_ANALYZER_API_KEY=SecretStr("ta"),
    )
    inst = build_text_analyzer(s)
    assert isinstance(inst, OpenAICompatibleTextAnalyzer)


# ──────────────────────────────────────── build_voice_ingest_provider
def test_voice_provider_none_when_disabled() -> None:
    s = MasterSettings(_env_file=None, **_base_kwargs())  # type: ignore[arg-type]
    assert build_voice_ingest_provider(s) is None


def test_voice_provider_audio_analyzer() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        VOICE_PIPELINE="audio_analyzer",
        AUDIO_ANALYZER_API_KEY=SecretStr("aa"),
    )
    inst = build_voice_ingest_provider(s)
    assert isinstance(inst, GeminiAudioAnalyzer)


def test_voice_provider_split_pipeline() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        VOICE_PIPELINE="stt_then_text_analyzer",
        STT_API_KEY=SecretStr("ss"),
        TEXT_ANALYZER_API_KEY=SecretStr("ta"),
    )
    inst = build_voice_ingest_provider(s)
    assert isinstance(inst, GroqWhisperAudioAnalyzer)


# ──────────────────────────────────────────────── voice_ingest_summary
def test_summary_disabled() -> None:
    s = MasterSettings(_env_file=None, **_base_kwargs())  # type: ignore[arg-type]
    assert "disabled" in voice_ingest_summary(s)


def test_summary_audio_analyzer_includes_model() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        VOICE_PIPELINE="audio_analyzer",
        AUDIO_ANALYZER_API_KEY=SecretStr("aa"),
        AUDIO_ANALYZER_MODEL="gemini-2.5-flash",
    )
    summary = voice_ingest_summary(s)
    assert "audio_analyzer" in summary
    assert "gemini-2.5-flash" in summary


def test_summary_split_pipeline_includes_both_steps() -> None:
    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        **_base_kwargs(),
        VOICE_PIPELINE="stt_then_text_analyzer",
        STT_API_KEY=SecretStr("ss"),
        TEXT_ANALYZER_API_KEY=SecretStr("ta"),
    )
    summary = voice_ingest_summary(s)
    assert "whisper-large-v3-turbo" in summary
    assert "grok-4-1-fast" in summary
