"""Shared fixtures.

- `repo`: a SqliteRepo backed by a tempfile DB with migrate() already run.
- `seats`: a deterministic 9-seat lineup (5 humans + 4 LLMs by default).
- `frozen_rng`: random.Random(seed) for reproducible role shuffles.
- `_isolate_llm_trace_dir` (autouse, session): redirect the LLM trace
  output to a session-temp dir so a decider test that calls
  ``decider.decide()`` without entering ``trace_context`` never leaks
  rows into the repo's production ``logs/llm_calls/no_game/`` (one such
  leak accumulated 700+ stray ``actor=None`` lines in production
  before this fixture was added).
"""

from __future__ import annotations

import os
import random
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

from wolfbot.domain.models import Seat
from wolfbot.persistence.schema import migrate
from wolfbot.persistence.sqlite_repo import SqliteRepo


@pytest.fixture(scope="session", autouse=True)
def _isolate_llm_trace_dir(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """Pin ``WOLFBOT_LLM_TRACE_DIR`` to a per-session temp dir for every
    test. Function-scoped fixtures that override the env var (e.g. the
    ``trace_dir`` fixture in ``test_llm_service.py``) still take
    precedence — pytest's monkeypatch undoes its setenv at teardown,
    after which ``os.environ`` falls back to this session-level value.
    """
    sandbox = tmp_path_factory.mktemp("llm_trace_sandbox")
    prev = os.environ.get("WOLFBOT_LLM_TRACE_DIR")
    os.environ["WOLFBOT_LLM_TRACE_DIR"] = str(sandbox)
    try:
        yield sandbox
    finally:
        if prev is None:
            os.environ.pop("WOLFBOT_LLM_TRACE_DIR", None)
        else:
            os.environ["WOLFBOT_LLM_TRACE_DIR"] = prev


@pytest.fixture
def frozen_rng() -> random.Random:
    return random.Random(42)


@pytest.fixture
def seats() -> list[Seat]:
    personas = ["setsu", "gina", "sq", "raqio"]
    out: list[Seat] = []
    for i in range(1, 10):
        if i <= 5:
            out.append(
                Seat(
                    seat_no=i,
                    display_name=f"Human{i}",
                    discord_user_id=f"user{i}",
                    is_llm=False,
                    persona_key=None,
                )
            )
        else:
            persona = personas[i - 6]
            out.append(
                Seat(
                    seat_no=i,
                    display_name=persona.title(),
                    discord_user_id=None,
                    is_llm=True,
                    persona_key=persona,
                )
            )
    return out


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[SqliteRepo]:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        await migrate(db_path)
        r = SqliteRepo(db_path)
        await r.connect()
        try:
            yield r
        finally:
            await r.close()
