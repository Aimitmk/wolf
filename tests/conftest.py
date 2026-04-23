"""Shared fixtures.

- `repo`: a SqliteRepo backed by a tempfile DB with migrate() already run.
- `seats`: a deterministic 9-seat lineup (5 humans + 4 LLMs by default).
- `frozen_rng`: random.Random(seed) for reproducible role shuffles.
"""

from __future__ import annotations

import random
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from wolfbot.domain.models import Seat
from wolfbot.persistence.schema import migrate
from wolfbot.persistence.sqlite_repo import SqliteRepo


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
