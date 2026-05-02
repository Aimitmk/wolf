"""Speech-to-text adapter Protocol + pluggable provider implementations.

Providers:
- ``GeminiAudioAnalyzer`` — sends raw audio to a cheap Gemini model
  (e.g. gemini-2.0-flash-lite) and gets back transcription + structured
  analysis (summary, claimed role, vote target, stance) in one API call.
  This is the default for voice-ingest because it eliminates a separate
  STT + LLM hop.
- ``GeminiSttService`` — skeleton; delegates to a user-supplied callable.
- ``FakeSttService`` — deterministic stub for tests.

The ``SttService`` Protocol is the injection seam used by
``VoiceIngestService``. Any implementation satisfying the Protocol can be
swapped in via configuration.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from wolfbot.domain.enums import (
    CO_CLAIM_VALUES,
    ROLE_CALLOUT_VALUES,
    VILLAGE_SIZE,
    format_co_claim_options,
)
from wolfbot.llm.template import render_template

# (seat_no, display_name) pair used to ground the analyzer's
# ``addressed_name`` extraction in the actual VC roster. Passing this
# at transcribe time lets the LLM resolve STT misspellings (e.g.
# Whisper's "ラッキーオ" for the spoken "ラキオ") to one of the real
# seats instead of returning the literal mistranscription that the
# downstream resolver can't match.
RosterEntry = tuple[int, str]

log = logging.getLogger(__name__)


def _seat_range_label() -> str:
    """1-based inclusive seat range derived from :data:`VILLAGE_SIZE`."""
    return f"1〜{VILLAGE_SIZE}"


def _coerce_seat_no(value: Any) -> int | None:
    """Coerce an analyzer-emitted seat number to ``int`` in [1, VILLAGE_SIZE].

    Accepts both ``int`` and string-of-digits because some models hand
    back numeric strings even when the JSON schema asks for an int.
    Returns ``None`` for any out-of-range / non-numeric input rather
    than raising — the analyzer is best-effort and the downstream
    name-resolver still has the literal ``addressed_name`` to fall
    back on.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        seat = value
    elif isinstance(value, str) and value.strip().isdigit():
        seat = int(value.strip())
    else:
        return None
    return seat if 1 <= seat <= VILLAGE_SIZE else None


_ADDRESSED_SEAT_FIELD_INSTRUCTION = (
    "**addressed_seat_no**: addressed_name と同じ人物の席番号(整数 1〜9)、"
    "または null。**downstream は addressed_name の文字一致より先に "
    "addressed_seat_no を採用する**ので、roster 上の人物に確信があるなら "
    "必ず席番号を埋める。確信が無いとき / 全体宛 / 該当無しは null。\n"
)


def _format_roster_block(roster: Sequence[RosterEntry] | None) -> str:
    """Render the participant roster as a prompt block.

    Empty when ``roster`` is None or empty so the static prompt
    sections look identical for callers that don't supply a roster
    (tests, voicetest in no-op mode). When non-empty, lists every
    seat with its ``display_name`` and instructs the analyzer LLM
    to resolve ``addressed_name`` / ``vote_target_seat`` to one of
    these canonical entries even when the upstream STT mangles the
    spelling - the typical "Whisper rendered 'ラキオ' as 'ラッキーオ'"
    failure mode this guard exists to fix.
    """
    if not roster:
        return ""
    lines = "\n".join(f"  - 席{seat}: {name}" for seat, name in roster)
    return (
        "\n現在の参加者(席番号 → display_name):\n"
        f"{lines}\n"
        "**addressed_name の表記揺れ正規化**: addressed_name は必ず上の "
        "display_name のいずれか(emoji含む完全一致)を返す、または null。"
        "STT の書き起こしは表記揺れすることがあるので以下を吸収して最も近い人物に対応付ける:\n"
        "- 音韻的に近い表記(例: 「ラッキーオ」「ラキ」「らきお」 → 「🦋ラキオ」)、"
        "濁音/半濁音/促音/長音/ヤ行イ行差/カタカナ⇔ひらがなを許容\n"
        "- さん/くん/ちゃん/様/氏 等の敬称は剝がして照合\n"
        "- 「3番」「席3」のような席番号呼びかけは、その席の display_name に解決\n"
        "- 「みんな」「全員」「みなさん」「お前ら」など全体宛は null\n"
        "- 候補が決められない場合も null(無理に当てない)\n"
        "**vote_target_seat も同じ参加者リストに従い、上の席番号のいずれか**を返す、"
        "または null。\n"
        + _ADDRESSED_SEAT_FIELD_INSTRUCTION
    )


def pcm_to_wav(
    pcm: bytes,
    *,
    sample_rate: int = 48_000,
    channels: int = 2,
    sample_width: int = 2,
) -> bytes:
    """Wrap raw PCM in a minimal RIFF/WAV header.

    Groq's ``audio/transcriptions`` endpoint runs ffmpeg under the hood
    and rejects raw PCM with ``HTTP 400`` even when the multipart
    ``Content-Type`` claims ``audio/wav``. The fix is to give it a real
    WAV file. Defaults match ``discord-ext-voice_recv``'s opus decoder
    output (48 kHz, stereo, 16-bit signed little-endian) so callers
    feeding straight from :class:`WolfbotAudioSink` need not pass
    explicit format kwargs.
    """
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sample_width)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


@dataclass(frozen=True)
class SttResult:
    """Outcome of a single STT call.

    `text` is empty on a low-confidence drop; `confidence` is set so callers
    can apply their own threshold check before deciding to emit a SpeechEvent.
    Hard provider failures raise SttProviderError instead.

    `summary` is an optional structured analysis of the utterance content,
    populated by providers that combine STT + inference in one call (e.g.
    GeminiAudioAnalyzer). Providers that only do transcription leave it None.

    `co_declaration` is a structured CO tag (`seer` / `medium` / `knight`)
    extracted from the utterance by providers that infer it (Gemini's
    `co_claim`). Authoritative when set; otherwise the discussion service
    falls back to substring matching on `text` for legacy compatibility.

    `raw_analysis` carries the full parsed JSON dict from the analyzer
    LLM (or the multimodal analyzer's structured output). This is
    pure debug visibility — production code paths read the typed
    fields above. The optional debug dump uses it to surface
    ``vote_target_seat``, ``stance``, and any future analyzer fields
    in the ``.txt`` sidecar without needing a separate cross-reference
    to the JSONL trace.
    """

    text: str
    confidence: float
    duration_ms: int
    summary: str | None = None
    co_declaration: str | None = None
    addressed_name: str | None = None
    # Seat number the analyzer resolved ``addressed_name`` to, when
    # the prompt was grounded with a roster. Bypasses the downstream
    # ``resolve_seat_by_name`` string match - critical when the live
    # VC display name (which the speaker hears and the analyzer is
    # told about) differs from ``Seat.display_name`` in the DB
    # (which is the persona's canonical handle and the only thing
    # the legacy resolver knows). ``None`` when no roster was given
    # or the analyzer couldn't pick a seat.
    addressed_seat_no: int | None = None
    # `role_callout` flags speeches that explicitly call for a specific
    # role to come out (e.g. "占い師の方は名乗り出てください"). The
    # downstream arbiter / NPC prompt builder uses this so real role
    # holders and wolf-side fakers can react. ``None`` for the vast
    # majority of utterances.
    role_callout: str | None = None
    raw_analysis: dict[str, Any] | None = None


class SttProviderError(RuntimeError):
    """Raised on hard STT failures (timeout, 5xx, malformed response).

    Carries a `failure_reason` matching the canonical voice-ingest enum
    (`stt_provider_error`, `stt_timeout`, etc.).
    """

    def __init__(self, failure_reason: str) -> None:
        super().__init__(failure_reason)
        self.failure_reason = failure_reason


@runtime_checkable
class SttService(Protocol):
    """Async STT adapter.

    Implementations MUST be cancellable and MUST NOT block the asyncio loop
    on the network call (use `asyncio.to_thread` or an async HTTP client).

    ``roster`` is an optional list of ``(seat_no, display_name)`` pairs
    grounding the analyzer LLM's ``addressed_name`` /
    ``vote_target_seat`` extraction. When supplied, the analyzer maps
    misspelled / phonetically-near transcriptions back to a canonical
    seat. Implementations should fall back to their static prompt
    when ``roster`` is None or empty.
    """

    async def transcribe(
        self,
        *,
        audio: bytes,
        language: str,
        timeout_s: float,
        roster: Sequence[RosterEntry] | None = None,
    ) -> SttResult: ...


class FakeSttService:
    """In-memory STT for tests.

    Either return a scripted sequence of results or raise scripted errors.
    Records the most-recently-passed ``roster`` so tests asserting on
    prompt-conditioning behavior can verify it without intercepting
    HTTP traffic.
    """

    def __init__(
        self,
        scripted: list[SttResult | Exception] | None = None,
        default: SttResult | None = None,
    ) -> None:
        self._scripted: list[SttResult | Exception] = list(scripted or [])
        self._default: SttResult | None = default
        self.call_count = 0
        self.last_roster: Sequence[RosterEntry] | None = None

    async def transcribe(
        self,
        *,
        audio: bytes,
        language: str,
        timeout_s: float,
        roster: Sequence[RosterEntry] | None = None,
    ) -> SttResult:
        self.call_count += 1
        self.last_roster = roster
        if self._scripted:
            head = self._scripted.pop(0)
            if isinstance(head, Exception):
                raise head
            return head
        if self._default is None:
            raise SttProviderError("stt_no_script")
        return self._default


class GeminiSttService:
    """Production Gemini API STT adapter.

    The real API call is delegated to a user-supplied `transcribe_fn` so
    the actual transport (Google Generative SDK or raw HTTP) is configurable
    and the hot path stays test-friendly. This module deliberately does NOT
    import the `google.generativeai` SDK at module level so test environments
    without those credentials still load this file.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        transcribe_fn: Callable[[bytes, str, str, float],
                                Awaitable[SttResult]] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._transcribe_fn = transcribe_fn

    async def transcribe(
        self,
        *,
        audio: bytes,
        language: str,
        timeout_s: float,
        roster: Sequence[RosterEntry] | None = None,
    ) -> SttResult:
        if self._transcribe_fn is None:
            raise SttProviderError("stt_provider_not_configured")
        # ``GeminiSttService`` is a thin transcribe-only delegate; it
        # has no analyzer prompt to inject the roster into. Tests that
        # care about roster propagation use ``GeminiAudioAnalyzer``
        # instead. Accept the param for Protocol conformance.
        del roster
        return await self._transcribe_fn(audio, language, self.model, timeout_s)


class GeminiAudioAnalyzer:
    """Gemini multimodal audio → transcription + structured analysis.

    Sends raw PCM/WAV audio directly to a cheap Gemini model and asks for
    both transcription and a structured JSON analysis in a single request.
    This replaces separate STT → LLM hops with one API call.

    The structured output includes:
    - ``transcript``: verbatim Japanese text
    - ``summary``: 1-sentence gist of what the speaker said
    - ``confidence``: self-assessed transcription confidence (0.0-1.0)
    - ``co_claim``: role CO if any (``seer``/``medium``/``knight``/null)
    - ``vote_target_seat``: seat number the speaker wants to execute (null if none)
    - ``stance``: dict of seat → trust (``positive``/``negative``/``neutral``)

    Uses ``httpx`` for the Gemini REST API to stay async-native. Model
    defaults to ``gemini-2.0-flash-lite`` (cheapest multimodal, ~$0.075/1M
    input tokens). Does NOT import ``httpx`` at module level.
    """

    _SYSTEM_PROMPT_TEMPLATE = "master/stt_audio_analyzer"

    @classmethod
    def _build_system_prompt(
        cls, roster: Sequence[RosterEntry] | None
    ) -> str:
        """Static template body + (optional) roster grounding block.

        Body lives in `prompts/templates/master/stt_audio_analyzer.md`
        with `{{co_claim_options}}` / `{{seat_range_label}}` filled
        from the canonical Python constants. The roster block is
        appended outside the template (it's a runtime list rendered
        per-game) so the template stays static.
        """
        return render_template(
            cls._SYSTEM_PROMPT_TEMPLATE,
            co_claim_options=format_co_claim_options(),
            seat_range_label=_seat_range_label(),
        ) + _format_roster_block(roster)

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.0-flash-lite",
        api_base: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_s: float = 15.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.timeout_s = timeout_s

    async def transcribe(
        self,
        *,
        audio: bytes,
        language: str,
        timeout_s: float,
        roster: Sequence[RosterEntry] | None = None,
    ) -> SttResult:
        import base64
        import json

        import httpx

        from wolfbot.services.llm_trace import (
            CallTimer,
            extract_gemini_rest_tokens,
            log_llm_call,
        )

        audio_b64 = base64.b64encode(audio).decode("ascii")
        effective_timeout = min(timeout_s, self.timeout_s)
        system_prompt = self._build_system_prompt(roster)

        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "audio/wav",
                                "data": audio_b64,
                            }
                        },
                        {
                            "text": system_prompt,
                        },
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
            },
        }

        url = (
            f"{self.api_base}/models/{self.model}:generateContent"
            f"?key={self.api_key}"
        )

        timer = CallTimer()
        raw_text = ""
        err: str | None = None
        tokens: dict[str, int | None] | None = None
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                resp = await client.post(url, json=body)
                if resp.status_code != 200:
                    err = f"gemini_http_{resp.status_code}"
                    raise SttProviderError(err)

                resp_json = resp.json()
                tokens = extract_gemini_rest_tokens(resp_json)
                raw_text = (
                    resp_json.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )

                parsed = self._parse_response(raw_text)
                transcript = parsed.get("transcript", "")
                confidence = float(parsed.get("confidence", 0.0))

                # Build a compact summary JSON from the structured fields
                summary_dict = {
                    k: v
                    for k, v in parsed.items()
                    if k not in ("transcript", "confidence") and v is not None and v != {}
                }
                summary_str = json.dumps(
                    summary_dict, ensure_ascii=False) if summary_dict else None

                co_raw = parsed.get("co_claim")
                co_declaration = (
                    co_raw if co_raw in CO_CLAIM_VALUES else None
                )

                addressed_raw = parsed.get("addressed_name")
                addressed_name: str | None = None
                if isinstance(addressed_raw, str):
                    stripped = addressed_raw.strip()
                    addressed_name = stripped or None
                addressed_seat_no = _coerce_seat_no(parsed.get("addressed_seat_no"))

                callout_raw = parsed.get("role_callout")
                role_callout = (
                    callout_raw if callout_raw in ROLE_CALLOUT_VALUES else None
                )

                # Estimate duration from audio size (assume 16kHz 16-bit mono WAV)
                data_bytes = max(0, len(audio) - 44)
                duration_ms = int(data_bytes / (16_000 * 2) * 1000)

                return SttResult(
                    text=transcript,
                    confidence=confidence,
                    duration_ms=duration_ms,
                    summary=summary_str,
                    co_declaration=co_declaration,
                    addressed_name=addressed_name,
                    addressed_seat_no=addressed_seat_no,
                    role_callout=role_callout,
                    raw_analysis=parsed or None,
                )

        except SttProviderError:
            raise
        except httpx.TimeoutException as exc:
            err = "gemini_timeout"
            raise SttProviderError(err) from exc
        except httpx.ConnectError as exc:
            err = "gemini_connection_refused"
            raise SttProviderError(err) from exc
        except Exception as exc:
            err = f"gemini_unexpected_{type(exc).__name__}"
            raise SttProviderError(err) from exc
        finally:
            await log_llm_call(
                role="voice_stt",
                provider="gemini",
                model=self.model,
                system_prompt=system_prompt,
                user_prompt=f"[audio bytes={len(audio)} mime=audio/wav]",
                response=raw_text or None,
                latency_ms=timer.elapsed_ms,
                error=err,
                tokens=tokens,
                extra={"audio_bytes": len(audio), "language": language},
            )

    @staticmethod
    def _parse_response(raw: str) -> dict:  # type: ignore[type-arg]
        """Best-effort parse of Gemini's JSON response.

        Gemini sometimes wraps the JSON in markdown fences or adds
        trailing text. We strip those and try ``json.loads``.
        """
        import json

        text = raw.strip()
        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            # Drop first and last fence lines
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            log.warning("gemini_json_parse_failed raw=%s", text[:200])
            return {"transcript": text, "confidence": 0.3}


class GroqWhisperAudioAnalyzer:
    """Two-step STT: Groq Whisper (transcribe) → analyzer LLM (extract).

    Groq's free tier on ``whisper-large-v3-turbo`` is generous and the
    transport is OpenAI-compatible (``audio/transcriptions`` multipart),
    so this slots in next to the existing Gemini analyzer without
    changing :class:`VoiceIngestService`. The structured fields the
    discussion path expects (``co_claim``, ``vote_target_seat``,
    ``addressed_name``, summary) are filled by a second call to a tiny
    OpenAI-compatible analyzer (xAI Grok in production) — the same
    contract the multimodal Gemini call returns in one hop.

    Both steps are traced under ``role=voice_stt`` so the exporter and
    viewer keep working without a schema change. The two trace lines are
    distinguished by ``metadata.step`` (``transcribe`` vs ``analyze``).

    Failure semantics: Whisper failure raises ``SttProviderError`` with
    a precise reason (timeout / 4xx / 5xx). Analyzer failure is
    soft-handled — we still surface the transcript with empty structured
    fields, because the discussion path can re-derive CO via legacy
    substring matching on the text. This keeps the human-speech signal
    flowing even when the analyzer LLM is briefly down.
    """

    _ANALYZER_PROMPT_TEMPLATE = "master/stt_text_analyzer"

    @classmethod
    def _build_analyzer_prompt(
        cls, roster: Sequence[RosterEntry] | None
    ) -> str:
        """Static template body + (optional) roster grounding block.

        Body lives in `prompts/templates/master/stt_text_analyzer.md`.
        The roster block is appended outside the template (per-game
        runtime list) so the template stays static.
        """
        return render_template(
            cls._ANALYZER_PROMPT_TEMPLATE,
            co_claim_options=format_co_claim_options(),
            seat_range_label=_seat_range_label(),
        ) + _format_roster_block(roster)

    def __init__(
        self,
        *,
        groq_api_key: str,
        groq_model: str = "whisper-large-v3-turbo",
        groq_base_url: str = "https://api.groq.com/openai/v1",
        analyzer_api_key: str,
        analyzer_model: str,
        analyzer_base_url: str = "https://api.x.ai/v1",
        timeout_s: float = 15.0,
        # Format of the raw PCM bytes ``transcribe()`` receives. Defaults
        # match discord-ext-voice_recv's opus decoder so the typical
        # caller (``WolfbotAudioSink`` → ``VoiceIngestService``) needs no
        # configuration. Override only when feeding pre-processed audio.
        pcm_sample_rate: int = 48_000,
        pcm_channels: int = 2,
        pcm_sample_width: int = 2,
    ) -> None:
        self.groq_api_key = groq_api_key
        self.groq_model = groq_model
        self.groq_base_url = groq_base_url.rstrip("/")
        self.analyzer_api_key = analyzer_api_key
        self.analyzer_model = analyzer_model
        self.analyzer_base_url = analyzer_base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.pcm_sample_rate = pcm_sample_rate
        self.pcm_channels = pcm_channels
        self.pcm_sample_width = pcm_sample_width

    async def transcribe(
        self,
        *,
        audio: bytes,
        language: str,
        timeout_s: float,
        roster: Sequence[RosterEntry] | None = None,
    ) -> SttResult:
        effective_timeout = min(timeout_s, self.timeout_s)
        bytes_per_sec = (
            self.pcm_sample_rate * self.pcm_channels * self.pcm_sample_width
        )
        duration_ms = (
            int(len(audio) / bytes_per_sec * 1000) if bytes_per_sec else 0
        )
        # Groq's whisper endpoint rejects headerless PCM. Wrap to WAV
        # using the configured PCM format so ffmpeg on Groq's side can
        # demux the stream.
        wav_audio = pcm_to_wav(
            audio,
            sample_rate=self.pcm_sample_rate,
            channels=self.pcm_channels,
            sample_width=self.pcm_sample_width,
        )

        transcript, asr_confidence = await self._whisper(
            wav_audio, language, effective_timeout
        )
        if not transcript:
            return SttResult(
                text="",
                confidence=0.0,
                duration_ms=duration_ms,
                summary=None,
                co_declaration=None,
                addressed_name=None,
            )

        analysis = await self._analyze(transcript, effective_timeout, roster=roster)
        co_raw = analysis.get("co_claim")
        co_decl = co_raw if co_raw in CO_CLAIM_VALUES else None
        addressed = analysis.get("addressed_name")
        addressed_name = (
            addressed.strip() or None
            if isinstance(addressed, str)
            else None
        )
        callout_raw = analysis.get("role_callout")
        role_callout_val = (
            callout_raw if callout_raw in ROLE_CALLOUT_VALUES else None
        )
        summary_dict = {
            k: v
            for k, v in analysis.items()
            if k not in ("summary", "confidence")
            and v is not None
            and v != {}
        }
        summary_str: str | None = None
        if "summary" in analysis and isinstance(analysis["summary"], str):
            summary_str = analysis["summary"]
        elif summary_dict:
            import json as _json

            summary_str = _json.dumps(summary_dict, ensure_ascii=False)

        return SttResult(
            text=transcript,
            # Use Whisper's ASR confidence (derived from
            # ``no_speech_prob``), NOT the analyzer's "claim clarity"
            # field — the latter legitimately returns 0.0 for greetings
            # and short reactions, which would silently fail the
            # ``confidence_threshold`` gate in ``VoiceIngestService``
            # and drop valid speech events. The analyzer's confidence
            # was a legacy field from the multimodal Gemini path where
            # both signals collapsed into one number.
            confidence=asr_confidence,
            duration_ms=duration_ms,
            summary=summary_str,
            co_declaration=co_decl,
            addressed_name=addressed_name,
            addressed_seat_no=_coerce_seat_no(analysis.get("addressed_seat_no")),
            role_callout=role_callout_val,
            raw_analysis=analysis or None,
        )

    async def _whisper(
        self, audio: bytes, language: str, timeout: float
    ) -> tuple[str, float]:
        """Step 1: POST audio to Groq's whisper transcription endpoint.

        Returns ``(transcript, asr_confidence)`` where ``asr_confidence``
        is derived from ``verbose_json``'s per-segment ``no_speech_prob``
        (a Whisper internal signal: probability that the segment is
        actually silence/noise rather than speech). Aggregating over
        segments gives a per-utterance ASR-side confidence that's
        independent of the downstream analyzer LLM. ``no_speech_prob``
        is in [0, 1]; we report ``1 - max(no_speech_prob)`` so a single
        bad segment lowers confidence appropriately.
        """
        import httpx

        from wolfbot.services.llm_trace import (
            CallTimer,
            log_llm_call,
        )

        url = f"{self.groq_base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.groq_api_key}"}
        # Whisper accepts BCP-47 like "ja"; map "ja-JP" → "ja".
        lang_code = language.split("-")[0] if language else None
        files: dict[str, tuple[str, bytes, str] | tuple[None, str]] = {
            "file": ("segment.wav", audio, "audio/wav"),
            "model": (None, self.groq_model),
            # ``verbose_json`` adds ``segments`` with ``no_speech_prob``;
            # plain ``json`` only carries ``text``. The size overhead is
            # ~1 KB per call, negligible vs the audio upload itself.
            "response_format": (None, "verbose_json"),
        }
        if lang_code:
            files["language"] = (None, lang_code)

        timer = CallTimer()
        transcript = ""
        confidence = 0.0
        err: str | None = None
        # Capture the upstream error body so a recurring 400/4xx is
        # diagnosable from the trace alone (status code by itself didn't
        # tell us "audio decode failed" the first time around).
        err_body: str | None = None
        tokens: dict[str, int | None] | None = None
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, files=files)
                if resp.status_code != 200:
                    err = f"groq_http_{resp.status_code}"
                    err_body = resp.text[:1000] if resp.text else None
                    raise SttProviderError(err)
                resp_json = resp.json()
                transcript = (resp_json.get("text") or "").strip()
                segments = resp_json.get("segments") or []
                # ``no_speech_prob`` may be missing on some segments
                # (e.g. when the response shape varies per Groq build);
                # fall back to a permissive 1.0 if the field is absent.
                if segments:
                    worst_no_speech = max(
                        float(s.get("no_speech_prob") or 0.0) for s in segments
                    )
                    confidence = max(0.0, min(1.0, 1.0 - worst_no_speech))
                else:
                    # No segments in the response (very short audio, or a
                    # response shape we don't recognize) — fall back to
                    # treating any returned transcript as confident.
                    confidence = 1.0 if transcript else 0.0
                # Groq returns an `x_groq.id` etc. but no usage field for
                # whisper; leave tokens=None like the openai SDK does.
        except SttProviderError:
            raise
        except httpx.TimeoutException as exc:
            err = "groq_timeout"
            raise SttProviderError(err) from exc
        except httpx.ConnectError as exc:
            err = "groq_connection_refused"
            raise SttProviderError(err) from exc
        except Exception as exc:
            err = f"groq_unexpected_{type(exc).__name__}"
            raise SttProviderError(err) from exc
        finally:
            await log_llm_call(
                role="voice_stt",
                provider="groq",
                model=self.groq_model,
                system_prompt=None,
                user_prompt=f"[audio bytes={len(audio)} mime=audio/wav lang={lang_code}]",
                response=transcript if err is None else err_body,
                latency_ms=timer.elapsed_ms,
                error=err,
                tokens=tokens,
                extra={
                    "audio_bytes": len(audio),
                    "step": "transcribe",
                    "asr_confidence": round(confidence, 3) if err is None else None,
                },
            )
        return transcript, confidence

    async def _analyze(
        self,
        transcript: str,
        timeout: float,
        *,
        roster: Sequence[RosterEntry] | None = None,
    ) -> dict:  # type: ignore[type-arg]
        """Step 2: ask the analyzer LLM to extract structured fields.

        Soft-fail: any error returns ``{}`` so the discussion path still
        sees the transcript via legacy substring CO matching. The trace
        line still records the error so operators can spot a chronic
        analyzer outage.
        """
        import json

        import httpx

        from wolfbot.services.llm_trace import (
            CallTimer,
            extract_openai_tokens,
            log_llm_call,
        )

        url = f"{self.analyzer_base_url}/chat/completions"
        analyzer_prompt = self._build_analyzer_prompt(roster)
        body = {
            "model": self.analyzer_model,
            "messages": [
                {"role": "system", "content": analyzer_prompt},
                {"role": "user", "content": transcript},
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.analyzer_api_key}",
            "Content-Type": "application/json",
        }

        timer = CallTimer()
        raw = ""
        err: str | None = None
        tokens: dict[str, int | None] | None = None
        parsed: dict = {}  # type: ignore[type-arg]
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
                if resp.status_code != 200:
                    err = f"analyzer_http_{resp.status_code}"
                    return {}
                resp_json = resp.json()
                # Extract OpenAI-shaped usage if present.
                from types import SimpleNamespace

                usage_raw = resp_json.get("usage") or {}
                usage_ns = SimpleNamespace(
                    prompt_tokens=usage_raw.get("prompt_tokens"),
                    completion_tokens=usage_raw.get("completion_tokens"),
                    total_tokens=usage_raw.get("total_tokens"),
                )
                tokens = extract_openai_tokens(SimpleNamespace(usage=usage_ns))
                raw = (
                    resp_json.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    or ""
                )
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    err = "analyzer_json_parse_failed"
                    parsed = {}
                return parsed
        except httpx.TimeoutException:
            err = "analyzer_timeout"
            return {}
        except httpx.ConnectError:
            err = "analyzer_connection_refused"
            return {}
        except Exception as exc:
            err = f"analyzer_unexpected_{type(exc).__name__}"
            return {}
        finally:
            await log_llm_call(
                role="voice_stt",
                provider="xai",
                model=self.analyzer_model,
                system_prompt=analyzer_prompt,
                user_prompt=transcript,
                response=raw or None,
                latency_ms=timer.elapsed_ms,
                error=err,
                tokens=tokens,
                extra={"step": "analyze"},
            )


__all__ = [
    "FakeSttService",
    "GeminiAudioAnalyzer",
    "GeminiSttService",
    "GroqWhisperAudioAnalyzer",
    "RosterEntry",
    "SttProviderError",
    "SttResult",
    "SttService",
]
