"""Factories for the three ingest components: ``AudioAnalyzer`` / ``Stt``
/ ``TextAnalyzer``.

The pipeline shape switches (``VOICE_PIPELINE``, ``TEXT_PIPELINE``) on
:class:`wolfbot.config.MasterSettings` decide which components a
particular boot needs; this module turns those switches plus the
component-scoped credentials into ready-to-inject instances.

Why a separate module: ``main.py`` and ``voicetest/main.py`` both need
exactly the same component construction. Centralising it here also keeps
the wiring in ``main.py`` flat — one ``build_*`` call per component
instead of nested provider branches per call site.

The functions return ``None`` when the pipeline doesn't request the
component, so callers can write ``if audio_analyzer is None:`` rather
than re-checking the pipeline shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from wolfbot.config import MasterSettings
    from wolfbot.master.state.text_analyzer import TextAnalyzer


def build_text_analyzer(settings: MasterSettings) -> TextAnalyzer | None:
    """Construct the ``TextAnalyzer`` instance shared by the text and
    split-voice pipelines, or ``None`` if neither pipeline needs it.

    The settings validator already guarantees ``TEXT_ANALYZER_API_KEY``
    is non-None when this returns non-None, so the assert is purely a
    type-narrowing aid for mypy.
    """
    needs_text_analyzer = (
        settings.TEXT_PIPELINE == "text_analyzer"
        or settings.VOICE_PIPELINE == "stt_then_text_analyzer"
    )
    if not needs_text_analyzer:
        return None
    assert settings.TEXT_ANALYZER_API_KEY is not None  # validator-guaranteed

    if settings.TEXT_ANALYZER_PROVIDER == "gemini":
        from wolfbot.master.state.text_analyzer import GeminiTextAnalyzer

        return GeminiTextAnalyzer(
            api_key=settings.TEXT_ANALYZER_API_KEY.get_secret_value(),
            model=settings.TEXT_ANALYZER_MODEL,
        )

    # xai / deepseek / openai all share the OpenAI Chat Completions surface.
    from wolfbot.master.state.text_analyzer import OpenAICompatibleTextAnalyzer

    base_url = settings.TEXT_ANALYZER_BASE_URL or _openai_compat_default_base_url(
        settings.TEXT_ANALYZER_PROVIDER
    )
    return OpenAICompatibleTextAnalyzer(
        api_key=settings.TEXT_ANALYZER_API_KEY.get_secret_value(),
        model=settings.TEXT_ANALYZER_MODEL,
        base_url=base_url,
    )


def build_voice_ingest_provider(settings: MasterSettings) -> Any | None:
    """Construct the ``SttService``-shaped object that
    :class:`VoiceIngestService` consumes, or ``None`` when voice ingest
    is disabled.

    Returns one of:

    * :class:`GeminiAudioAnalyzer` — when ``VOICE_PIPELINE=audio_analyzer``.
    * :class:`GroqWhisperAudioAnalyzer` — when
      ``VOICE_PIPELINE=stt_then_text_analyzer``. Composes the Stt
      provider with the OpenAI-compatible ``TextAnalyzer`` settings;
      the analyzer step shares its credentials with the text-channel
      ``TextAnalyzer`` so both paths hit the same RPM bucket.
    * ``None`` — when ``VOICE_PIPELINE=disabled``.

    Both return types satisfy the ``SttService`` Protocol used by
    :mod:`wolfbot.master.voice.voice_ingest_service`. Returning ``Any``
    here avoids dragging the heavy ``stt_service`` module into modules
    that only need the credential-presence check.
    """
    if settings.VOICE_PIPELINE == "disabled":
        return None

    if settings.VOICE_PIPELINE == "audio_analyzer":
        assert settings.AUDIO_ANALYZER_API_KEY is not None  # validator-guaranteed
        from wolfbot.master.voice.stt_service import GeminiAudioAnalyzer

        return GeminiAudioAnalyzer(
            api_key=settings.AUDIO_ANALYZER_API_KEY.get_secret_value(),
            model=settings.AUDIO_ANALYZER_MODEL,
        )

    # VOICE_PIPELINE == "stt_then_text_analyzer"
    assert settings.STT_API_KEY is not None  # validator-guaranteed
    assert settings.TEXT_ANALYZER_API_KEY is not None  # validator-guaranteed
    from wolfbot.master.voice.stt_service import GroqWhisperAudioAnalyzer

    analyzer_base_url = (
        settings.TEXT_ANALYZER_BASE_URL
        or _openai_compat_default_base_url(settings.TEXT_ANALYZER_PROVIDER)
    )
    return GroqWhisperAudioAnalyzer(
        groq_api_key=settings.STT_API_KEY.get_secret_value(),
        groq_model=settings.STT_MODEL,
        groq_base_url=settings.STT_BASE_URL,
        analyzer_api_key=settings.TEXT_ANALYZER_API_KEY.get_secret_value(),
        analyzer_model=settings.TEXT_ANALYZER_MODEL,
        analyzer_base_url=analyzer_base_url,
    )


def voice_ingest_summary(settings: MasterSettings) -> str:
    """Human-readable wiring summary for boot-time logs.

    Mirrors the strings ``main.py`` previously logged inline so operators
    grepping for ``integrated voice-ingest wired`` keep a similar signal.
    """
    if settings.VOICE_PIPELINE == "disabled":
        return "voice_pipeline=disabled"
    if settings.VOICE_PIPELINE == "audio_analyzer":
        return (
            f"voice_pipeline=audio_analyzer "
            f"provider={settings.AUDIO_ANALYZER_PROVIDER} "
            f"model={settings.AUDIO_ANALYZER_MODEL}"
        )
    return (
        f"voice_pipeline=stt_then_text_analyzer "
        f"stt={settings.STT_PROVIDER}:{settings.STT_MODEL} "
        f"analyzer={settings.TEXT_ANALYZER_PROVIDER}:{settings.TEXT_ANALYZER_MODEL}"
    )


_OPENAI_COMPAT_DEFAULTS: dict[str, str] = {
    "xai": "https://api.x.ai/v1",
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
}


def _openai_compat_default_base_url(provider: str) -> str:
    """Default base URL for an OpenAI-compatible provider.

    Falls back to the xAI URL for unknown providers because the analyzer
    code path is identical regardless of host (any error surfaces at the
    first request rather than at construction).
    """
    return _OPENAI_COMPAT_DEFAULTS.get(provider, "https://api.x.ai/v1")


__all__ = [
    "build_text_analyzer",
    "build_voice_ingest_provider",
    "voice_ingest_summary",
]
