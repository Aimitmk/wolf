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
    """A minimal blob shaped like 16kHz mono WAV (44-byte header + zeros)."""
    return b"\x00" * (44 + payload_size)


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


async def test_full_pipeline_success_propagates_structured_fields(
    trace_dir: Path, patch_httpx: Any
) -> None:
    patch_httpx({
        "https://api.groq.com/": {
            "status": 200,
            "json": {"text": "席3が怪しいから投票する"},
        },
        "https://api.x.ai/": {
            "status": 200,
            "json": _analyzer_json({
                "summary": "席3への投票表明",
                "confidence": 0.92,
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
    assert result.confidence == pytest.approx(0.92)
    assert result.summary == "席3への投票表明"
    assert result.co_declaration is None
    assert result.addressed_name is None
    # 4000 PCM bytes / 32000 bytes/sec = 0.125s
    assert 100 < result.duration_ms < 200

    rows = _read_voice_stt_trace(trace_dir)
    assert len(rows) == 2
    transcribe_row = next(r for r in rows if r["metadata"]["step"] == "transcribe")
    analyze_row = next(r for r in rows if r["metadata"]["step"] == "analyze")
    assert transcribe_row["provider"] == "groq"
    assert transcribe_row["model"] == "whisper-large-v3-turbo"
    assert transcribe_row["error"] is None
    assert "席3" in transcribe_row["response"]
    assert analyze_row["provider"] == "xai"
    assert analyze_row["model"] == "grok-4-1-fast-non-reasoning"
    assert analyze_row["tokens"] == {"prompt": 80, "completion": 30, "total": 110}


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
            "json": {"text": "占いCO 席7白"},
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
    assert r.confidence == pytest.approx(0.9)  # default fallback
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


async def test_duration_ms_estimated_from_pcm_size(patch_httpx: Any) -> None:
    """A 1-second WAV (16000 samples * 2 bytes + 44-byte header) → ~1000 ms."""
    patch_httpx({
        "https://api.groq.com/": {"status": 200, "json": {"text": "短く"}},
        "https://api.x.ai/": {"status": 200, "json": _analyzer_json({})},
    })
    one_second = b"\x00" * (44 + 16_000 * 2)
    a = GroqWhisperAudioAnalyzer(
        groq_api_key="g", groq_model="m",
        analyzer_api_key="x", analyzer_model="grok",
    )
    r = await a.transcribe(audio=one_second, language="ja-JP", timeout_s=5)
    assert 950 <= r.duration_ms <= 1050
