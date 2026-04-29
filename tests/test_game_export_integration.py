"""Integration test: real SQLite schema → exporter → viewer-shaped JSON.

Goes the full distance from production-shape DB (via the same
``migrate()`` that runs at boot) through ``export_game`` and asserts the
output round-trips through the canonical Pydantic models without losing
or fabricating fields. Catches three classes of regression at once:

1. **DB schema drift** — if a column the exporter SELECTs gets renamed
   or dropped from ``schema.py``, this test fails because the SQL errors.
2. **Exporter shape drift** — the output is fed back into
   :class:`GameExport`; any new ``extra="forbid"`` violation or missing
   required field is caught here.
3. **Viewer contract drift** — the committed
   ``viewer/sample-data/export-schema.json`` is compared against
   ``GameExport.model_json_schema()``; running
   ``scripts/dump-export-schema.py`` is now a hard requirement after
   touching ``game_export_types.py``.

The first two assertions exercise the exact code path
``GameService._on_game_end_finalize`` uses in production, so a green
test guarantees the live exporter — invoked on every victory and
``/wolf abort`` — produces viewer-loadable JSON.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import aiosqlite
import pytest

from wolfbot.persistence.schema import migrate
from wolfbot.services.game_export import export_game
from wolfbot.services.game_export_types import GameExport
from wolfbot.services.llm_trace import log_llm_call, trace_context

GAME_ID = "g_int_test_1"


# Columns the exporter SELECTs from each table. If any of these go missing
# from schema.py, the SELECT errors and this test fails. The list is the
# explicit DB-side contract — keep in sync with game_export.py:_build_payload.
_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "games": {
        "id", "guild_id", "host_user_id", "discussion_mode",
        "created_at", "ended_at", "main_text_channel_id",
        "main_vc_channel_id",
    },
    "seats": {
        "seat_no", "display_name", "is_llm", "persona_key",
        "discord_user_id", "role", "alive", "death_cause", "death_day",
    },
    "logs_public": {"day", "phase", "kind", "actor_seat", "text", "created_at"},
    "votes": {"day", "round", "voter_seat", "target_seat", "submitted_at"},
    "night_actions": {
        "day", "actor_seat", "kind", "target_seat", "submitted_at",
    },
    "speech_events": {
        "event_id", "day", "phase", "source", "speaker_seat", "text",
        "stt_confidence", "summary", "co_declaration",
        "addressed_seat_no", "created_at_ms",
    },
}


async def _seed_realistic_game(db_path: Path) -> None:
    """Populate every table the exporter reads with realistic 9-player data.

    Uses raw SQL so the test is decoupled from ``SqliteRepo``'s API shape
    and exercises only the on-disk schema — the same surface the exporter
    sees in production.
    """
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        await db.execute(
            "INSERT INTO games (id, guild_id, host_user_id, phase, day_number, "
            "main_text_channel_id, main_vc_channel_id, created_at, ended_at, "
            "discussion_mode) "
            "VALUES (?, ?, ?, 'GAME_OVER', 2, ?, ?, ?, ?, ?)",
            (GAME_ID, "guild_int", "host_int", "ch_text", "ch_vc",
             1_700_000_000, 1_700_001_000, "reactive_voice"),
        )

        seats = [
            (1, "human1", "Alice", 0, None, "VILLAGER", 1, None, None),
            (2, None, "Setsu", 1, "setsu", "SEER", 1, None, None),
            (3, None, "Gina", 1, "gina", "WEREWOLF", 0, "EXECUTION", 1),
            (4, None, "SQ", 1, "sq", "VILLAGER", 1, None, None),
            (5, None, "Raqio", 1, "raqio", "MEDIUM", 1, None, None),
            (6, None, "Stella", 1, "stella", "KNIGHT", 1, None, None),
            (7, None, "Shigemichi", 1, "shigemichi", "WEREWOLF", 0,
             "EXECUTION", 2),
            (8, None, "Comet", 1, "comet", "MADMAN", 1, None, None),
            (9, None, "Jonas", 1, "jonas", "VILLAGER", 1, None, None),
        ]
        for s in seats:
            await db.execute(
                "INSERT INTO seats (game_id, seat_no, discord_user_id, "
                "display_name, is_llm, persona_key, role, alive, death_cause, "
                "death_day) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (GAME_ID, *s),
            )

        public_logs = [
            (0, "SETUP", "SETUP_COMPLETE", None, "ゲーム開始", 1_700_000_001),
            (1, "DAY_DISCUSSION", "PHASE_CHANGE", None, "1 日目議論",
             1_700_000_100),
            (1, "DAY_VOTE", "EXECUTION", 3,
             "席3 が処刑されました\n\n席3=5票", 1_700_000_500),
            (2, "DAY_VOTE", "EXECUTION", 7,
             "席7 が処刑されました\n\n席7=7票", 1_700_000_900),
            (2, "DAY_VOTE", "VICTORY", None, "村人陣営の勝利!",
             1_700_000_910),
            (2, "DAY_VOTE", "ROLE_REVEAL", None, "役職公開:\n席1=村人...",
             1_700_000_911),
        ]
        for day, phase, kind, actor, text, ts in public_logs:
            await db.execute(
                "INSERT INTO logs_public (game_id, day, phase, kind, "
                "actor_seat, text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (GAME_ID, day, phase, kind, actor, text, ts),
            )

        votes = [
            (1, 0, 1, 3, 1_700_000_400),
            (1, 0, 2, 3, 1_700_000_405),
            (1, 0, 3, 2, 1_700_000_410),
            (1, 0, 4, 3, 1_700_000_415),
            (1, 0, 5, 3, 1_700_000_420),
            (1, 0, 6, 3, 1_700_000_425),
            (1, 0, 7, 2, 1_700_000_430),
            (1, 0, 8, 2, 1_700_000_435),
            (1, 0, 9, 3, 1_700_000_440),
            # Day 2 with one vote that targets None (skip / abstain)
            (2, 0, 1, 7, 1_700_000_800),
            (2, 0, 8, None, 1_700_000_805),
        ]
        for v in votes:
            await db.execute(
                "INSERT INTO votes (game_id, day, round, voter_seat, "
                "target_seat, submitted_at) VALUES (?, ?, ?, ?, ?, ?)",
                (GAME_ID, *v),
            )

        night_actions = [
            (0, 2, "SEER_DIVINE", 9, 1_700_000_050),
            (1, 2, "SEER_DIVINE", 7, 1_700_000_550),
            (1, 6, "KNIGHT_GUARD", 2, 1_700_000_555),
            (1, 7, "WOLF_ATTACK", 2, 1_700_000_560),
        ]
        for na in night_actions:
            await db.execute(
                "INSERT INTO night_actions (game_id, day, actor_seat, kind, "
                "target_seat, submitted_at) VALUES (?, ?, ?, ?, ?, ?)",
                (GAME_ID, *na),
            )

        speech_events = [
            ("ev_d1_seat2", "DAY_DISCUSSION", "npc_generated", "npc", 2,
             "占い師COします。", None, "seat2 CO seer", "seer", None,
             1_700_000_150_000),
            ("ev_d1_seat3", "DAY_DISCUSSION", "text", "human", 3,
             "私も占いです。", None, "seat3 counter", "seer", 2,
             1_700_000_160_000),
            ("ev_d1_voice_seat1", "DAY_DISCUSSION", "voice_stt", "human", 1,
             "席3怪しい", 0.92, None, None, 3,
             1_700_000_170_000),
            # `phase_baseline` is the internal sentinel — exporter MUST
            # filter it. Seeded so the test asserts the SQL filter.
            ("ev_d1_baseline", "DAY_DISCUSSION", "phase_baseline", "system",
             None, "", None, None, None, None, 1_700_000_140_000),
            ("ev_d2_seat5", "DAY_DISCUSSION", "npc_generated", "npc", 5,
             "霊媒結果:席3は人狼", None, "seat5 medium", "medium", None,
             1_700_000_700_000),
        ]
        for (eid, phase, source, kind, sseat, text, conf, summary, co,
             addr, ts) in speech_events:
            phase_id = f"{GAME_ID}::day1::{phase}::1"
            await db.execute(
                "INSERT INTO speech_events (event_id, game_id, phase_id, "
                "day, phase, source, speaker_kind, speaker_seat, text, "
                "stt_confidence, audio_start_ms, audio_end_ms, "
                "alive_seat_nos_json, summary, co_declaration, "
                "addressed_seat_no, created_at_ms) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (eid, GAME_ID, phase_id, 1 if "d1" in eid else 2, phase,
                 source, kind, sseat, text, conf, None, None, None,
                 summary, co, addr, ts),
            )

        await db.commit()


async def _stage_real_trace(trace_root: Path) -> None:
    """Use the production ``log_llm_call`` to write JSONL the exporter reads.

    Going through the real writer (rather than handwriting JSON) ensures
    the test breaks if ``log_llm_call`` ever changes the on-disk shape
    in a way the exporter's loader can't accept.
    """
    import os

    # Force trace dir to our tmp path; clear any disable flag.
    prev_dir = os.environ.get("WOLFBOT_LLM_TRACE_DIR")
    prev_disabled = os.environ.get("WOLFBOT_LLM_TRACE_DISABLED")
    os.environ["WOLFBOT_LLM_TRACE_DIR"] = str(trace_root)
    os.environ.pop("WOLFBOT_LLM_TRACE_DISABLED", None)
    try:
        with trace_context(
            game_id=GAME_ID,
            phase="DAY_DISCUSSION",
            day=1,
            actor="seat=2 persona=setsu role=SEER",
            metadata={"task": "discussion"},
        ):
            await log_llm_call(
                role="gameplay",
                provider="xai",
                model="grok-4-1-fast",
                system_prompt="あなたは占い師です。",
                user_prompt="現在のフェイズ: DAY_DISCUSSION (day=1)",
                response='{"intent":"speak","public_message":"占い師COします。"}',
                latency_ms=1234,
                tokens={"prompt": 100, "completion": 30, "total": 130},
            )
        with trace_context(
            game_id=GAME_ID,
            phase="DAY_DISCUSSION",
            day=1,
            actor="seat=2 persona=setsu",
        ):
            await log_llm_call(
                role="npc_speech",
                provider="openai-compat",
                model="grok-4-1-fast",
                system_prompt="NPCとして発話",
                user_prompt="短い反応",
                response='{"intent":"speak","text":"占い師COします"}',
                latency_ms=900,
                tokens={"prompt": 80, "completion": 20, "total": 100},
                file_stem="npc_setsu",
            )
        with trace_context(game_id=GAME_ID, phase=None, day=None):
            await log_llm_call(
                role="voice_stt",
                provider="gemini",
                model="gemini-2.0-flash-lite",
                system_prompt="音声を分析",
                user_prompt="[audio bytes=80000]",
                response='{"transcript":"席3怪しい"}',
                latency_ms=850,
                tokens={"prompt": 200, "completion": 25, "total": 225},
            )
    finally:
        if prev_dir is None:
            os.environ.pop("WOLFBOT_LLM_TRACE_DIR", None)
        else:
            os.environ["WOLFBOT_LLM_TRACE_DIR"] = prev_dir
        if prev_disabled is not None:
            os.environ["WOLFBOT_LLM_TRACE_DISABLED"] = prev_disabled


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    return tmp_path


async def test_real_db_schema_columns_cover_exporter_select(
    tmp_repo: Path,
) -> None:
    """A column listed in ``_REQUIRED_COLUMNS`` must exist after migrate().

    Cheaper than running the exporter — fires on the *first* drift between
    the DDL and the SELECTs in ``game_export.py``.
    """
    db_path = tmp_repo / "wolf.db"
    await migrate(db_path)
    async with aiosqlite.connect(str(db_path)) as db:
        for table, required in _REQUIRED_COLUMNS.items():
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                actual = {row[1] async for row in cur}
            missing = required - actual
            assert not missing, (
                f"schema.py is missing columns from {table}: {missing}. "
                "Either add them to schema.py or remove the SELECT in "
                "game_export.py."
            )


async def test_export_pipeline_against_live_schema(tmp_repo: Path) -> None:
    """End-to-end: real DDL + real INSERTs + real trace writer + real exporter.

    The output is parsed back through ``GameExport`` so any drift between
    the dict shape inside the exporter and the canonical Pydantic schema
    surfaces here.
    """
    db_path = tmp_repo / "wolf.db"
    await migrate(db_path)
    await _seed_realistic_game(db_path)
    trace_root = tmp_repo / "trace"
    await _stage_real_trace(trace_root)

    out_path = await export_game(
        game_id=GAME_ID,
        db_path=db_path,
        trace_dir=trace_root,
        output_dir=tmp_repo / "out",
    )
    raw = out_path.read_text(encoding="utf-8")

    # Parse with the canonical Pydantic model — this is the contract the
    # viewer assumes. extra="forbid" on every model except TraceEntry
    # means an unexpected key fails this line.
    export = GameExport.model_validate_json(raw)

    # Game meta surfaces production-shape data correctly.
    assert export.game.id == GAME_ID
    assert export.game.discussion_mode == "reactive_voice"
    assert export.game.victory == "village"
    assert export.game.created_at_ms == 1_700_000_000_000
    assert export.game.ended_at_ms == 1_700_001_000_000

    # All 9 seats; roles round-trip; deaths attributed correctly.
    assert len(export.seats) == 9
    by_seat = {s.seat_no: s for s in export.seats}
    assert by_seat[3].alive is False
    assert by_seat[3].death_cause == "EXECUTION"
    assert by_seat[3].death_day == 1
    assert by_seat[7].death_cause == "EXECUTION"
    assert by_seat[7].death_day == 2
    assert by_seat[2].role == "SEER"
    assert by_seat[1].is_llm is False
    assert by_seat[2].is_llm is True

    # Every (day, phase) bucket the test seeded must show up exactly once.
    bucket_keys = [(p.day, p.phase) for p in export.phases]
    assert (0, "NIGHT_0") in bucket_keys  # from night_actions(day=0)
    assert (1, "DAY_DISCUSSION") in bucket_keys
    assert (1, "DAY_VOTE") in bucket_keys
    assert (1, "NIGHT") in bucket_keys
    assert (2, "DAY_VOTE") in bucket_keys
    assert len(bucket_keys) == len(set(bucket_keys)), "duplicate phase bucket"

    # Votes route to the right phase. Day 2 has a None target_seat (skip).
    d2 = next(p for p in export.phases if (p.day, p.phase) == (2, "DAY_VOTE"))
    assert {v.target_seat for v in d2.votes} == {7, None}

    # Speech events with all the structured fields populated.
    d1_disc = next(
        p for p in export.phases if (p.day, p.phase) == (1, "DAY_DISCUSSION")
    )
    voice_ev = next(
        ev for ev in d1_disc.speech_events if ev.source == "voice_stt"
    )
    assert voice_ev.stt_confidence == pytest.approx(0.92)
    assert voice_ev.addressed_seat_no == 3
    co_ev = next(
        ev for ev in d1_disc.speech_events if ev.co_declaration == "seer"
    )
    assert co_ev.speaker_seat in (2, 3)
    # phase_baseline rows are the internal sentinel — never surfaced to viewer.
    sources_seen = {ev.source for p in export.phases for ev in p.speech_events}
    assert "phase_baseline" not in sources_seen
    assert sources_seen <= {"text", "voice_stt", "npc_generated"}

    # All three trace roles came through. Order is chronological by ts.
    roles_seen = {t.role for t in export.trace}
    assert roles_seen == {"gameplay", "npc_speech", "voice_stt"}
    # file_stem is auto-injected from the JSONL filename.
    file_stems = {t.file_stem for t in export.trace}
    assert "gameplay" in file_stems
    assert "npc_setsu" in file_stems
    assert "voice_stt" in file_stems
    # Token usage round-trips.
    gameplay = next(t for t in export.trace if t.role == "gameplay")
    assert gameplay.tokens is not None
    assert gameplay.tokens.total == 130


async def test_committed_schema_matches_pydantic_models() -> None:
    """The viewer-side ``export-schema.json`` must equal the live schema.

    If this fails, run::

        uv run python scripts/dump-export-schema.py

    The viewer's contract test loads the committed file; out-of-date
    schemas there cause spurious failures or silently-accepted drift.
    """
    repo_root = Path(__file__).resolve().parents[1]
    committed_path = repo_root / "viewer" / "sample-data" / "export-schema.json"
    assert committed_path.is_file(), (
        f"missing {committed_path} — run scripts/dump-export-schema.py"
    )
    committed = json.loads(committed_path.read_text(encoding="utf-8"))
    live = GameExport.model_json_schema()
    assert committed == live, (
        "viewer/sample-data/export-schema.json is stale — "
        "run `uv run python scripts/dump-export-schema.py` to refresh."
    )


async def test_bundled_sample_validates_against_models() -> None:
    """``viewer/sample-data/game-sample.json`` must satisfy ``GameExport``."""
    repo_root = Path(__file__).resolve().parents[1]
    sample_path = repo_root / "viewer" / "sample-data" / "game-sample.json"
    if not sample_path.is_file():
        pytest.skip("sample not generated yet")
    GameExport.model_validate_json(sample_path.read_text(encoding="utf-8"))


async def test_export_round_trips_when_trace_dir_missing(
    tmp_repo: Path,
) -> None:
    """Pre-trace games still validate — empty trace list is the contract."""
    db_path = tmp_repo / "wolf.db"
    await migrate(db_path)
    await _seed_realistic_game(db_path)

    out_path = await export_game(
        game_id=GAME_ID,
        db_path=db_path,
        trace_dir=tmp_repo / "absent",
        output_dir=tmp_repo / "out",
    )
    export = GameExport.model_validate_json(out_path.read_text(encoding="utf-8"))
    assert export.trace == []


async def test_export_rejects_unknown_game(tmp_repo: Path) -> None:
    db_path = tmp_repo / "wolf.db"
    await migrate(db_path)
    with pytest.raises(ValueError, match="game not found"):
        await export_game(
            game_id="missing",
            db_path=db_path,
            trace_dir=tmp_repo / "trace",
            output_dir=tmp_repo / "out",
        )


async def test_malformed_trace_line_does_not_break_export(
    tmp_repo: Path,
) -> None:
    """One bad JSONL line is tolerated — the rest of the export still runs."""
    db_path = tmp_repo / "wolf.db"
    await migrate(db_path)
    await _seed_realistic_game(db_path)
    trace_root = tmp_repo / "trace"
    game_dir = trace_root / GAME_ID
    game_dir.mkdir(parents=True)
    # Mix of valid + malformed (json error) + schema-violating (string for
    # latency_ms). Valid line must come through; the rest are skipped.
    valid = json.dumps({
        "ts": "2026-04-28T08:00:00+00:00",
        "role": "gameplay",
        "provider": "xai",
        "model": "grok-4-1-fast",
        "phase": "DAY_DISCUSSION",
        "day": 1,
        "actor": "seat=2",
        "system_prompt": "s",
        "user_prompt": "u",
        "response": "r",
        "latency_ms": 100,
        "tokens": None,
        "error": None,
    })
    schema_violating = json.dumps({
        "ts": "2026-04-28T08:00:01+00:00",
        "role": "gameplay",
        "provider": "xai",
        "model": "x",
        "phase": None,
        "day": None,
        "actor": None,
        "system_prompt": "s",
        "user_prompt": "u",
        "response": "r",
        "latency_ms": "not-an-int",  # wrong type
        "tokens": None,
        "error": None,
    })
    (game_dir / "gameplay.jsonl").write_text(
        valid + "\n{not valid json\n" + schema_violating + "\n",
        encoding="utf-8",
    )

    # Tempfile setup
    with tempfile.TemporaryDirectory() as out_root:
        out_path = await export_game(
            game_id=GAME_ID,
            db_path=db_path,
            trace_dir=trace_root,
            output_dir=Path(out_root),
        )
        export = GameExport.model_validate_json(
            out_path.read_text(encoding="utf-8")
        )
    assert len(export.trace) == 1
    assert export.trace[0].role == "gameplay"
    assert export.trace[0].latency_ms == 100
