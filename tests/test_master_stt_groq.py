"""Groq Whisper STT pipeline tests.

Cover the two-step ``GroqWhisperAudioAnalyzer.transcribe`` pipeline:

1. **Whisper success → analyzer success** — both calls 200, structured
   fields propagate to ``SttResult``.
2. **Whisper success → analyzer 5xx** — soft fall-back: transcript
   surfaces, structured fields are ``None`` so the discussion path can
   still legacy-match CO via substring.
3. **Whisper 4xx/5xx** — hard error: ``SttProviderError`` raised, no
   analyzer call attempted.
4. **Whisper 200 with empty transcript** — short-circuits the pipeline
   so we don't burn an analyzer call on silence.
5. **Trace lines emitted** — both steps write under ``role=voice_stt``
   distinguished by ``metadata.step``; tokens captured for the analyzer
   step when the response carries usage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from wolfbot.master.stt_service import (
    GroqWhisperAudioAnalyzer,
    SttProviderError,
)


@pytest.fixture
def trace_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WOLFBOT_LLM_TRACE_DIR", str(tmp_path))
    monkeypatch.delenv("WOLFBOT_LLM_TRACE_DISABLED", raising=False)
    return tmp_path


def _make_wav(payload_size: int = 4_000) -> bytes:
    """An opaque PCM-shaped blob. Tests don't parse the actual bytes —
    the mock httpx layer just echoes a canned response. Defaults to
    4 000 zero bytes (~20 ms of Discord-native 48 kHz stereo PCM)."""
    return b"\x00" * payload_size


def _read_voice_stt_trace(trace_dir: Path, game_id: str = "g_groq") -> list[dict]:
    path = trace_dir / game_id / "voice_stt.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _httpx_mock(routes: dict[str, dict[str, Any]]) -> Any:
    """Return an ``httpx.MockTransport`` that dispatches by URL prefix.

    Each route value is ``{"status": int, "json": dict}`` (or ``"text"`` /
    ``"raise"`` for non-JSON paths). Multiple routes can match — we pick
    the longest-prefix match to disambiguate Groq vs analyzer URLs.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # Longest-prefix wins.
        candidates = [k for k in routes if url.startswith(k)]
        if not candidates:
            raise AssertionError(f"unexpected request: {url}")
        key = max(candidates, key=len)
        spec = routes[key]
        if "raise" in spec:
            raise spec["raise"]
        if "text" in spec:
            return httpx.Response(spec["status"], text=spec["text"])
        return httpx.Response(spec["status"], json=spec.get("json", {}))

    return httpx.MockTransport(handler)


@pytest.fixture
def patch_httpx(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace ``httpx.AsyncClient`` so each test can wire its own routes.

    Returns a setter function the test calls with the route dict.
    """
    state: dict[str, Any] = {"transport": None}

    real_client = httpx.AsyncClient

    class _MockedClient(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("transport", state["transport"])
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", _MockedClient)

    def set_routes(routes: dict[str, dict[str, Any]]) -> None:
        state["transport"] = _httpx_mock(routes)

    return set_routes


def _analyzer_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a structured-analysis payload as an OpenAI-compatible response."""
    return {
        "choices": [
            {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
        ],
        "usage": {
            "prompt_tokens": 80,
            "completion_tokens": 30,
            "total_tokens": 110,
        },
    }


def _whisper_verbose_json(text: str, no_speech_prob: float = 0.05) -> dict[str, Any]:
    """Shape Groq's ``verbose_json`` response: top-level ``text`` plus a
    single ``segments`` entry carrying ``no_speech_prob``. The transcribe
    code path computes ASR confidence as ``1 - max(no_speech_prob)``."""
    return {
        "text": text,
        "segments": [{"text": text, "no_speech_prob": no_speech_prob}],
    }


async def test_full_pipeline_success_propagates_structured_fields(
    trace_dir: Path, patch_httpx: Any
) -> None:
    patch_httpx({
        "https://api.groq.com/": {
            "status": 200,
            "json": _whisper_verbose_json("席3が怪しいから投票する", no_speech_prob=0.05),
        },
        "https://api.x.ai/": {
            "status": 200,
            "json": _analyzer_json({
                "summary": "席3への投票表明",
                "co_claim": None,
                "vote_target_seat": 3,
                "stance": {"3": "negative"},
                "addressed_name": None,
            }),
        },
    })

    analyzer = GroqWhisperAudioAnalyzer(
        groq_api_key="g_test",
        groq_model="whisper-large-v3-turbo",
        analyzer_api_key="x_test",
        analyzer_model="grok-4-1-fast-non-reasoning",
    )
    from wolfbot.services.llm_trace import trace_context

    with trace_context(game_id="g_groq", phase="DAY_DISCUSSION", day=1):
        result = await analyzer.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=10.0)

    assert result.text == "席3が怪しいから投票する"
    # confidence = 1 - 0.05 = 0.95, surfaced from Whisper not the analyzer.
    assert result.confidence == pytest.approx(0.95)
    assert result.summary == "席3への投票表明"
    assert result.co_declaration is None
    assert result.addressed_name is None
    # 4000 PCM bytes at Discord's native 48 kHz stereo 16-bit
    # (192_000 bytes/sec) ≈ 20 ms.
    assert 15 < result.duration_ms < 30

    rows = _read_voice_stt_trace(trace_dir)
    assert len(rows) == 2
    transcribe_row = next(r for r in rows if r["metadata"]["step"] == "transcribe")
    analyze_row = next(r for r in rows if r["metadata"]["step"] == "analyze")
    assert transcribe_row["provider"] == "groq"
    assert transcribe_row["model"] == "whisper-large-v3-turbo"
    assert transcribe_row["error"] is None
    assert "席3" in transcribe_row["response"]
    assert transcribe_row["metadata"]["asr_confidence"] == pytest.approx(0.95)
    assert analyze_row["provider"] == "xai"
    assert analyze_row["model"] == "grok-4-1-fast-non-reasoning"
    assert analyze_row["tokens"] == {"prompt": 80, "completion": 30, "total": 110}


async def test_low_claim_confidence_does_not_drop_speech(patch_httpx: Any) -> None:
    """Regression: the analyzer used to return ``confidence: 0.0`` for
    greetings and short utterances ("おやすみなさい"); we previously
    surfaced that as ``SttResult.confidence``, which made
    ``VoiceIngestService`` drop the segment as ``stt_low_confidence``
    and the user's voice never reached the arbiter. ASR confidence
    must come from Whisper's signal, not the analyzer's claim-clarity
    judgement."""
    patch_httpx({
        "https://api.groq.com/": {
            "status": 200,
            "json": _whisper_verbose_json("おやすみなさい", no_speech_prob=0.02),
        },
        # Analyzer returns no useful structured fields for a greeting,
        # but that must not gate the speech event.
        "https://api.x.ai/": {
            "status": 200,
            "json": _analyzer_json({
                "summary": "就寝の挨拶",
                "co_claim": None,
                "vote_target_seat": None,
                "stance": {},
                "addressed_name": None,
            }),
        },
    })
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    r = await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)
    assert r.text == "おやすみなさい"
    # 1 - 0.02 = 0.98; safely above any reasonable confidence_threshold.
    assert r.confidence > 0.9


async def test_high_no_speech_prob_lowers_confidence(patch_httpx: Any) -> None:
    """When Whisper itself thinks the audio is mostly silence/noise,
    confidence must drop so ``VoiceIngestService`` can filter via its
    threshold. This is the legitimate use case for the gate — silence
    bursts mis-detected as speech."""
    patch_httpx({
        "https://api.groq.com/": {
            "status": 200,
            "json": _whisper_verbose_json("ふぁ", no_speech_prob=0.85),
        },
        "https://api.x.ai/": {
            "status": 200,
            "json": _analyzer_json({}),
        },
    })
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    r = await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)
    # 1 - 0.85 = 0.15
    assert r.confidence == pytest.approx(0.15)


async def test_missing_segments_falls_back_to_full_confidence(patch_httpx: Any) -> None:
    """If Groq's response omits ``segments`` (very short audio, future
    response-shape change), we trust the transcript and report 1.0 —
    losing data quietly is worse than over-trusting on rare edge cases."""
    patch_httpx({
        "https://api.groq.com/": {
            "status": 200,
            "json": {"text": "やぁ"},  # no segments key
        },
        "https://api.x.ai/": {
            "status": 200,
            "json": _analyzer_json({}),
        },
    })
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    r = await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)
    assert r.text == "やぁ"
    assert r.confidence == pytest.approx(1.0)


async def test_co_declaration_normalizes_to_known_roles(patch_httpx: Any) -> None:
    """Analyzer can return arbitrary strings for ``co_claim``; only the
    three accepted roles surface on ``SttResult``."""
    patch_httpx({
        "https://api.groq.com/": {
            "status": 200,
            "json": {"text": "占いCO 席7白"},
        },
        "https://api.x.ai/": {
            "status": 200,
            "json": _analyzer_json({
                "summary": "占いCO",
                "confidence": 0.9,
                "co_claim": "seer",
                "vote_target_seat": None,
                "stance": {},
                "addressed_name": None,
            }),
        },
    })
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    r = await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)
    assert r.co_declaration == "seer"


async def test_invalid_co_value_drops_to_none(patch_httpx: Any) -> None:
    patch_httpx({
        "https://api.groq.com/": {"status": 200, "json": {"text": "なにか"}},
        "https://api.x.ai/": {
            "status": 200,
            "json": _analyzer_json({"co_claim": "wolf"}),  # not in allowed set
        },
    })
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    r = await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)
    assert r.co_declaration is None


async def test_addressed_name_strips_and_normalizes(patch_httpx: Any) -> None:
    patch_httpx({
        "https://api.groq.com/": {"status": 200, "json": {"text": "  ジナさんどう?"}},
        "https://api.x.ai/": {
            "status": 200,
            "json": _analyzer_json({"addressed_name": "  ジナさん  "}),
        },
    })
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    r = await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)
    assert r.addressed_name == "ジナさん"


async def test_empty_transcript_short_circuits_analyzer(
    trace_dir: Path, patch_httpx: Any
) -> None:
    """A blank transcription must NOT trigger the analyzer call — no
    point burning a Grok request on silence."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "groq" in url:
            return httpx.Response(200, json={"text": ""})
        return httpx.Response(200, json=_analyzer_json({}))

    import wolfbot.master.stt_service as stt_module  # noqa: F401

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    class _M(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            k["transport"] = transport
            super().__init__(*a, **k)

    import httpx as httpx_mod

    httpx_mod.AsyncClient = _M  # type: ignore[misc]
    try:
        a = GroqWhisperAudioAnalyzer(
            groq_api_key="g", groq_model="m",
            analyzer_api_key="x", analyzer_model="grok",
        )
        from wolfbot.services.llm_trace import trace_context

        with trace_context(game_id="g_groq"):
            r = await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)
    finally:
        httpx_mod.AsyncClient = real  # type: ignore[misc]

    assert r.text == ""
    assert r.confidence == 0.0
    assert all("groq" in c for c in calls), f"analyzer was called for empty transcript: {calls}"
    rows = _read_voice_stt_trace(trace_dir)
    assert len(rows) == 1
    assert rows[0]["metadata"]["step"] == "transcribe"


async def test_whisper_429_raises_stt_provider_error(patch_httpx: Any) -> None:
    """Hard failures from Whisper bubble up as ``SttProviderError`` so
    voice_ingest can route through the documented failure path."""
    patch_httpx({
        "https://api.groq.com/": {"status": 429, "json": {"error": "rate"}},
        "https://api.x.ai/": {"status": 200, "json": _analyzer_json({})},
    })
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    with pytest.raises(SttProviderError) as exc_info:
        await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)
    assert exc_info.value.failure_reason == "groq_http_429"


async def test_whisper_4xx_response_body_recorded_in_trace(
    trace_dir: Path, patch_httpx: Any
) -> None:
    """A non-200 from Groq must surface its response body in the trace
    so a recurring failure mode (e.g. 'audio decode failed') is
    diagnosable from the JSONL alone — the original HTTP-400 incident
    only logged the status code, masking the real cause."""
    patch_httpx({
        "https://api.groq.com/": {
            "status": 400,
            "text": "audio decode failed: invalid wav header",
        },
        "https://api.x.ai/": {"status": 200, "json": _analyzer_json({})},
    })
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    from wolfbot.services.llm_trace import trace_context

    with (
        trace_context(game_id="g_groq"),
        pytest.raises(SttProviderError),
    ):
        await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)

    rows = _read_voice_stt_trace(trace_dir)
    transcribe_row = next(r for r in rows if r["metadata"]["step"] == "transcribe")
    assert transcribe_row["error"] == "groq_http_400"
    assert transcribe_row["response"] is not None
    assert "audio decode failed" in transcribe_row["response"]


async def test_whisper_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def raiser(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    transport = httpx.MockTransport(raiser)
    real = httpx.AsyncClient

    class _M(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            k["transport"] = transport
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", _M)

    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    with pytest.raises(SttProviderError) as exc_info:
        await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)
    assert exc_info.value.failure_reason == "groq_timeout"


async def test_analyzer_5xx_soft_fails_keeps_transcript(
    trace_dir: Path, patch_httpx: Any
) -> None:
    """Analyzer outage must NOT lose the transcript — the discussion path
    can still derive CO via substring, and dropping the speech entirely
    would silence the human in front of the bot."""
    patch_httpx({
        "https://api.groq.com/": {
            "status": 200,
            "json": _whisper_verbose_json("占いCO 席7白", no_speech_prob=0.1),
        },
        "https://api.x.ai/": {"status": 503, "text": "upstream"},
    })
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    from wolfbot.services.llm_trace import trace_context

    with trace_context(game_id="g_groq"):
        r = await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)

    assert r.text == "占いCO 席7白"
    # ASR confidence comes from Whisper (1 - 0.1 = 0.9), not from the
    # analyzer — analyzer outage doesn't change the transcription's
    # confidence.
    assert r.confidence == pytest.approx(0.9)
    assert r.summary is None
    assert r.co_declaration is None

    rows = _read_voice_stt_trace(trace_dir)
    assert len(rows) == 2
    analyze_row = next(r for r in rows if r["metadata"]["step"] == "analyze")
    assert analyze_row["error"] == "analyzer_http_503"
    assert analyze_row["response"] is None


async def test_analyzer_returns_garbage_json_soft_fails(
    trace_dir: Path, patch_httpx: Any
) -> None:
    """Malformed analyzer JSON must not crash the pipeline — record the
    parse error in the trace and fall through with the transcript."""
    patch_httpx({
        "https://api.groq.com/": {"status": 200, "json": {"text": "やあ"}},
        "https://api.x.ai/": {
            "status": 200,
            "json": {
                "choices": [{"message": {"content": "not-json{"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        },
    })
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    from wolfbot.services.llm_trace import trace_context

    with trace_context(game_id="g_groq"):
        r = await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)

    assert r.text == "やあ"
    assert r.summary is None
    rows = _read_voice_stt_trace(trace_dir)
    analyze_row = next(r for r in rows if r["metadata"]["step"] == "analyze")
    assert analyze_row["error"] == "analyzer_json_parse_failed"
    # Even on parse fail we record the raw response so operators can debug.
    assert analyze_row["response"] == "not-json{"


async def test_raw_pcm_is_wrapped_to_wav_before_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: production sends Discord-native raw PCM. Without the
    WAV wrapper, Groq returns ``HTTP 400`` because ffmpeg can't demux a
    headerless byte stream — observed in the 2026-04-28 game with 15
    consecutive ``groq_http_400`` failures. Assert the multipart
    payload contains a real RIFF/WAV header."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "groq" in url:
            captured["body"] = request.read()
            return httpx.Response(200, json={"text": "テスト"})
        return httpx.Response(200, json=_analyzer_json({}))

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    class _M(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            k["transport"] = transport
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", _M)

    raw_pcm = b"\x00" * 9_600  # 50 ms of 48 kHz stereo
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g",
        groq_model="m",
        analyzer_api_key="x",
        analyzer_model="grok",
    )
    await a.transcribe(audio=raw_pcm, language="ja-JP", timeout_s=5)

    body = captured["body"]
    # Multipart form contains the file part; the wrapped audio MUST start
    # with the standard ``RIFF`` magic and contain a ``WAVE`` marker.
    assert b"RIFF" in body
    assert b"WAVE" in body
    # And the configured sample rate (48000 = 0x0000BB80 little-endian)
    # appears in the fmt chunk.
    assert b"\x80\xbb\x00\x00" in body


async def test_language_strips_region_for_whisper(patch_httpx: Any) -> None:
    """Whisper accepts BCP-47 short codes (``ja``) not regional ones
    (``ja-JP``); the adapter must strip before sending."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "groq" in url:
            captured["body"] = request.read()
            return httpx.Response(200, json={"text": "テスト"})
        return httpx.Response(200, json=_analyzer_json({}))

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    class _M(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            k["transport"] = transport
            super().__init__(*a, **k)

    import httpx as httpx_mod

    httpx_mod.AsyncClient = _M  # type: ignore[misc]
    try:
        a = GroqWhisperAudioAnalyzer(
            groq_api_key="g", groq_model="m",
            analyzer_api_key="x", analyzer_model="grok",
        )
        await a.transcribe(audio=_make_wav(), language="ja-JP", timeout_s=5)
    finally:
        httpx_mod.AsyncClient = real  # type: ignore[misc]

    body = captured["body"]
    # multipart payload contains form field "language" with value "ja"
    assert b'name="language"' in body
    # "ja-JP" should NOT appear — we stripped to "ja".
    assert b"ja-JP" not in body


async def test_duration_ms_estimated_from_pcm_format(patch_httpx: Any) -> None:
    """``duration_ms`` is computed from the configured PCM format (default
    Discord native: 48 kHz stereo 16-bit). 1 second of audio = 192 000
    bytes."""
    patch_httpx({
        "https://api.groq.com/": {"status": 200, "json": {"text": "短く"}},
        "https://api.x.ai/": {"status": 200, "json": _analyzer_json({})},
    })
    one_second_native = b"\x00" * (48_000 * 2 * 2)
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    r = await a.transcribe(audio=one_second_native, language="ja-JP", timeout_s=5)
    assert 950 <= r.duration_ms <= 1050


async def test_duration_ms_honors_overridden_pcm_format(patch_httpx: Any) -> None:
    """A caller feeding 16 kHz mono PCM (e.g. a future down-mixer)
    overrides the format kwargs and gets a correct duration."""
    patch_httpx({
        "https://api.groq.com/": {"status": 200, "json": {"text": "短く"}},
        "https://api.x.ai/": {"status": 200, "json": _analyzer_json({})},
    })
    one_second_16k_mono = b"\x00" * (16_000 * 2)
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
        pcm_sample_rate=16_000,
        pcm_channels=1,
    )
    r = await a.transcribe(audio=one_second_16k_mono, language="ja-JP", timeout_s=5)
    assert 950 <= r.duration_ms <= 1050
