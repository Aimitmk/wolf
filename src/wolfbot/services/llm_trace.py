"""Append-only JSONL trace of every LLM call.

Captures gameplay LLM (vote / night-action / discussion text), NPC speech
LLM, and voice STT/Analyzer calls into per-game JSONL files so a finished
game can be replayed end-to-end with full prompt + response visibility.

Layout (relative to cwd, override via ``WOLFBOT_LLM_TRACE_DIR``)::

    logs/llm_calls/
      no_game/                  # calls before any game context is set
        voice_stt.jsonl
      {game_id}/
        gameplay.jsonl          # gameplay-LLM deciders (xAI / DeepSeek / Gemini)
        npc_{persona}.jsonl     # NPC bot speech generation, one file per persona
        voice_stt.jsonl         # Master-side STT/audio analysis

Each JSONL line is one call::

    {
      "ts": "2026-04-28T13:14:55.123+00:00",
      "role": "gameplay" | "npc_speech" | "voice_stt",
      "provider": "xai" | "deepseek" | "gemini" | "openai-compat",
      "model": "grok-4-1-fast",
      "phase": "DAY_DISCUSSION",
      "day": 2,
      "actor": "seat=4 persona=setsu role=WEREWOLF",
      "metadata": {"task": "vote"},
      "system_prompt": "...",
      "user_prompt": "...",
      "response": "...",          # raw text (JSON string for structured-output models)
      "latency_ms": 1234,
      "error": null
    }

Concurrency: one ``asyncio.Lock`` per file path so concurrent appends from
parallel LLM seats interleave at line boundaries.

Disable: ``WOLFBOT_LLM_TRACE_DISABLED=1``.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_DIR = Path("logs/llm_calls")

# Context vars carrying game/phase/actor identifiers from the calling code
# down into the decider / generator without changing public Protocols.
_game_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wolfbot_llm_trace_game_id", default=None
)
_phase_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wolfbot_llm_trace_phase", default=None
)
_day_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "wolfbot_llm_trace_day", default=None
)
_actor_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wolfbot_llm_trace_actor", default=None
)
_metadata_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "wolfbot_llm_trace_metadata", default=None
)

_file_locks: dict[str, asyncio.Lock] = {}


def trace_enabled() -> bool:
    return os.environ.get("WOLFBOT_LLM_TRACE_DISABLED") != "1"


def trace_base_dir() -> Path:
    raw = os.environ.get("WOLFBOT_LLM_TRACE_DIR")
    return Path(raw) if raw else _DEFAULT_DIR


def parse_game_id_from_phase_id(phase_id: str | None) -> str | None:
    """Extract ``game_id`` from canonical phase_id ``"{gid}::dayN::PHASE::seq"``."""
    if not phase_id:
        return None
    head, sep, _ = phase_id.partition("::")
    return head if sep else None


@contextmanager
def trace_context(
    *,
    game_id: str | None = None,
    phase: str | None = None,
    day: int | None = None,
    actor: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[None]:
    """Set trace context for the duration of a block.

    Wrap a logical unit of work (e.g. one ``LLMAdapter._ask`` call) so every
    LLM call nested inside inherits the same identifiers without changing
    decider Protocols.
    """
    tokens: list[tuple[contextvars.ContextVar[Any], contextvars.Token[Any]]] = []
    if game_id is not None:
        tokens.append((_game_id_ctx, _game_id_ctx.set(game_id)))
    if phase is not None:
        tokens.append((_phase_ctx, _phase_ctx.set(phase)))
    if day is not None:
        tokens.append((_day_ctx, _day_ctx.set(day)))
    if actor is not None:
        tokens.append((_actor_ctx, _actor_ctx.set(actor)))
    if metadata is not None:
        tokens.append((_metadata_ctx, _metadata_ctx.set(metadata)))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


class CallTimer:
    """Lightweight stopwatch used at every LLM call site."""

    __slots__ = ("_t0",)

    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)


async def log_llm_call(
    *,
    role: str,
    provider: str,
    model: str,
    system_prompt: str | None,
    user_prompt: str | None,
    response: str | None,
    latency_ms: int,
    error: str | None = None,
    actor: str | None = None,
    extra: dict[str, Any] | None = None,
    file_stem: str | None = None,
    tokens: dict[str, int | None] | None = None,
) -> None:
    """Append one trace line. Never raises — failures are logged and dropped.

    ``role`` selects the default file stem (``"{role}.jsonl"``); pass
    ``file_stem`` to override (e.g. NPC bots use ``"npc_setsu"``).

    ``tokens`` carries token usage as ``{"prompt": N, "completion": N, "total": N}``
    when the provider returned usage metadata, ``None`` otherwise.
    """
    if not trace_enabled():
        return
    try:
        gid = _game_id_ctx.get() or "no_game"
        gid_safe = _sanitize(gid)
        stem_safe = _sanitize(file_stem or role)
        base = trace_base_dir() / gid_safe
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{stem_safe}.jsonl"

        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "role": role,
            "provider": provider,
            "model": model,
            "phase": _phase_ctx.get(),
            "day": _day_ctx.get(),
            "actor": actor or _actor_ctx.get(),
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response": response,
            "latency_ms": latency_ms,
            "tokens": tokens,
            "error": error,
        }
        ctx_md = _metadata_ctx.get()
        if ctx_md or extra:
            entry["metadata"] = {**(ctx_md or {}), **(extra or {})}
        line = json.dumps(entry, ensure_ascii=False) + "\n"

        lock = _file_locks.setdefault(str(path), asyncio.Lock())
        async with lock:
            await asyncio.to_thread(_append_line, path, line)
    except Exception:
        log.exception("llm_trace_write_failed role=%s model=%s", role, model)


def _append_line(path: Path, line: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def extract_openai_tokens(resp: Any) -> dict[str, int | None] | None:
    """Pull ``{prompt, completion, total}`` from an OpenAI-compatible response.

    Used by the xAI / DeepSeek / OpenAI-compat paths. Returns ``None`` when
    the response has no usage attribute (e.g. test fakes), so trace lines
    stay valid even without real token data.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    return {
        "prompt": getattr(usage, "prompt_tokens", None),
        "completion": getattr(usage, "completion_tokens", None),
        "total": getattr(usage, "total_tokens", None),
    }


def extract_gemini_vertex_tokens(resp: Any) -> dict[str, int | None] | None:
    """Pull ``{prompt, completion, total}`` from a google-genai response.

    Vertex Gemini exposes ``resp.usage_metadata.{prompt_token_count,
    candidates_token_count, total_token_count}`` — different field names
    from OpenAI but the same three integers conceptually.
    """
    um = getattr(resp, "usage_metadata", None)
    if um is None:
        return None
    return {
        "prompt": getattr(um, "prompt_token_count", None),
        "completion": getattr(um, "candidates_token_count", None),
        "total": getattr(um, "total_token_count", None),
    }


def extract_gemini_rest_tokens(resp_json: dict[str, Any]) -> dict[str, int | None] | None:
    """Pull token counts from the Gemini REST ``generateContent`` response."""
    um = resp_json.get("usageMetadata")
    if not isinstance(um, dict):
        return None
    return {
        "prompt": um.get("promptTokenCount"),
        "completion": um.get("candidatesTokenCount"),
        "total": um.get("totalTokenCount"),
    }


_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _sanitize(s: str) -> str:
    """Sanitize a path component (game_id or file stem).

    Dots are replaced too — preventing both ``..`` traversal and accidental
    extension confusion with the appended ``.jsonl`` suffix.
    """
    cleaned = _SAFE_RE.sub("_", s).strip("_")
    return cleaned or "x"


__all__ = [
    "CallTimer",
    "extract_gemini_rest_tokens",
    "extract_gemini_vertex_tokens",
    "extract_openai_tokens",
    "log_llm_call",
    "parse_game_id_from_phase_id",
    "trace_base_dir",
    "trace_context",
    "trace_enabled",
]
