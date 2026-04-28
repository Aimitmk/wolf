"""Tests for ``wolfbot.services.game_export``."""

from __future__ import annotations

import json
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from wolfbot.domain.enums import DeathCause, Phase, Role
from wolfbot.domain.models import (
    Game,
    LogEntry,
    NightAction,
    Seat,
    Vote,
)
from wolfbot.persistence.schema import migrate
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.game_export import export_game


@pytest_asyncio.fixture
async def fixture_repo() -> AsyncIterator[tuple[SqliteRepo, Path]]:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        await migrate(db_path)
        r = SqliteRepo(db_path)
        await r.connect()
        try:
            yield r, db_path
        finally:
            await r.close()


GAME_ID = "g_export_test"


async def _seed_minimal_game(repo: SqliteRepo) -> None:
    game = Game(
        id=GAME_ID,
        guild_id="guild",
        host_user_id="host",
        phase=Phase.GAME_OVER,
        day_number=1,
        deadline_epoch=None,
        main_text_channel_id="chan_text",
        main_vc_channel_id="chan_vc",
        heaven_channel_id=None,
        wolves_channel_id=None,
        created_at=1_700_000_000,
        ended_at=1_700_001_000,
        force_skip_pending=False,
        discussion_mode="rounds",
    )
    await repo.create_game(game)
    seats = [
        Seat(seat_no=1, display_name="Alice", is_llm=False, persona_key=None,
             discord_user_id="u1"),
        Seat(seat_no=2, display_name="Bot", is_llm=True, persona_key="setsu",
             discord_user_id="u2"),
    ]
    for seat in seats:
        await repo.insert_seat(GAME_ID, seat)
    # Roles + final alive/death state — set via the same path the live engine
    # uses (set_player_role + raw SQL nudge for death fields).
    await repo.set_player_role(GAME_ID, 1, Role.VILLAGER)
    await repo.set_player_role(GAME_ID, 2, Role.WEREWOLF)
    async with repo._tx() as db:
        await db.execute(
            "UPDATE seats SET alive=0, death_cause=?, death_day=? "
            "WHERE game_id=? AND seat_no=2",
            (DeathCause.EXECUTION.value, 1, GAME_ID),
        )

    # Public logs over two phases.
    await repo.insert_log_public(
        LogEntry(
            game_id=GAME_ID,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            kind="PHASE_CHANGE",
            actor_seat=None,
            visibility="PUBLIC",
            text="議論開始",
            created_at=1_700_000_100,
        )
    )
    await repo.insert_log_public(
        LogEntry(
            game_id=GAME_ID,
            day=1,
            phase=Phase.DAY_VOTE,
            kind="EXECUTION",
            actor_seat=2,
            visibility="PUBLIC",
            text="席2 が処刑されました\n\n席2=2票",
            created_at=1_700_000_500,
        )
    )
    await repo.insert_log_public(
        LogEntry(
            game_id=GAME_ID,
            day=1,
            phase=Phase.DAY_VOTE,
            kind="VICTORY",
            actor_seat=None,
            visibility="PUBLIC",
            text="村人陣営の勝利!",
            created_at=1_700_000_510,
        )
    )

    # One vote (round=0 is the regular vote in this schema; runoff is round=1).
    await repo.insert_vote(
        Vote(
            game_id=GAME_ID,
            day=1,
            round=0,
            voter_seat=1,
            target_seat=2,
            submitted_at=1_700_000_400,
        )
    )
    # One night action (day 0 wolf intro divine).
    from wolfbot.domain.enums import SubmissionType

    await repo.insert_night_action(
        NightAction(
            game_id=GAME_ID,
            day=0,
            actor_seat=2,
            kind=SubmissionType.SEER_DIVINE,
            target_seat=1,
            submitted_at=1_700_000_050,
        )
    )


async def test_export_game_writes_json_with_full_shape(
    fixture_repo: tuple[SqliteRepo, Path], tmp_path: Path
) -> None:
    repo, db_path = fixture_repo
    await _seed_minimal_game(repo)

    out = await export_game(
        game_id=GAME_ID,
        db_path=db_path,
        trace_dir=tmp_path / "no_trace",  # absent dir → empty trace
        output_dir=tmp_path / "out",
    )
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))

    # Game meta
    assert payload["game"]["id"] == GAME_ID
    assert payload["game"]["created_at_ms"] == 1_700_000_000_000
    assert payload["game"]["ended_at_ms"] == 1_700_001_000_000
    assert payload["game"]["victory"] == "village"
    assert payload["game"]["discussion_mode"] == "rounds"

    # Seats
    seats = payload["seats"]
    assert len(seats) == 2
    seat2 = next(s for s in seats if s["seat_no"] == 2)
    assert seat2["alive"] is False
    assert seat2["death_cause"] == "EXECUTION"
    assert seat2["death_day"] == 1
    assert seat2["role"] == "WEREWOLF"

    # Phases — chronological order, NIGHT_0 first (from night_actions),
    # then DAY_DISCUSSION, then DAY_VOTE.
    phases = payload["phases"]
    assert [(p["day"], p["phase"]) for p in phases] == [
        (0, "NIGHT_0"),
        (1, "DAY_DISCUSSION"),
        (1, "DAY_VOTE"),
    ]

    # Vote ended up under DAY_VOTE only, not under DAY_DISCUSSION.
    vote_phase = next(p for p in phases if p["phase"] == "DAY_VOTE")
    disc_phase = next(p for p in phases if p["phase"] == "DAY_DISCUSSION")
    assert len(vote_phase["votes"]) == 1
    assert vote_phase["votes"][0]["target_seat"] == 2
    assert vote_phase["votes"][0]["submitted_at_ms"] == 1_700_000_400_000
    assert disc_phase["votes"] == []

    # Night action ended up under NIGHT_0.
    night0 = next(p for p in phases if p["phase"] == "NIGHT_0")
    assert len(night0["night_actions"]) == 1
    assert night0["night_actions"][0]["kind"] == "SEER_DIVINE"

    # Public logs unconditionally serialized with ms timestamps.
    assert disc_phase["public_logs"][0]["created_at_ms"] == 1_700_000_100_000
    assert vote_phase["public_logs"][0]["kind"] == "EXECUTION"

    # Trace is empty when the trace_dir has no game subdirectory.
    assert payload["trace"] == []


async def test_export_game_inlines_jsonl_trace(
    fixture_repo: tuple[SqliteRepo, Path], tmp_path: Path
) -> None:
    repo, db_path = fixture_repo
    await _seed_minimal_game(repo)

    # Stage some trace files.
    trace_root = tmp_path / "trace"
    game_trace = trace_root / GAME_ID
    game_trace.mkdir(parents=True)
    (game_trace / "gameplay.jsonl").write_text(
        json.dumps({"ts": "2026-04-28T08:00:01+00:00", "role": "gameplay",
                    "model": "grok-4-1-fast", "system_prompt": "s",
                    "user_prompt": "u", "response": "r", "latency_ms": 100,
                    "tokens": {"prompt": 5, "completion": 1, "total": 6},
                    "phase": "DAY_DISCUSSION", "day": 1,
                    "actor": "seat=2 persona=setsu",
                    "provider": "xai", "error": None}) + "\n",
        encoding="utf-8",
    )
    (game_trace / "voice_stt.jsonl").write_text(
        json.dumps({"ts": "2026-04-28T08:00:00+00:00", "role": "voice_stt",
                    "provider": "gemini", "model": "gemini-2.0-flash-lite",
                    "system_prompt": "s", "user_prompt": "[audio]",
                    "response": "{}", "latency_ms": 800, "tokens": None,
                    "phase": None, "day": None, "actor": None,
                    "error": None}) + "\n",
        encoding="utf-8",
    )

    out = await export_game(
        game_id=GAME_ID,
        db_path=db_path,
        trace_dir=trace_root,
        output_dir=tmp_path / "out",
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    trace = payload["trace"]
    assert len(trace) == 2
    # Ordered chronologically by ts — voice_stt at 08:00:00 before gameplay 08:00:01
    assert trace[0]["role"] == "voice_stt"
    assert trace[1]["role"] == "gameplay"
    # file_stem is auto-injected from the source file name
    assert trace[0]["file_stem"] == "voice_stt"
    assert trace[1]["file_stem"] == "gameplay"


async def test_export_game_raises_for_unknown_game(
    fixture_repo: tuple[SqliteRepo, Path], tmp_path: Path
) -> None:
    _repo, db_path = fixture_repo
    with pytest.raises(ValueError, match="game not found"):
        await export_game(
            game_id="nonexistent",
            db_path=db_path,
            trace_dir=tmp_path,
            output_dir=tmp_path / "out",
        )
