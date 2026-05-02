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
        Seat(seat_no=1, display_name="Alice", is_llm=False, persona_key=None, discord_user_id="u1"),
        Seat(seat_no=2, display_name="Bot", is_llm=True, persona_key="setsu", discord_user_id="u2"),
    ]
    for seat in seats:
        await repo.insert_seat(GAME_ID, seat)
    # Roles + final alive/death state — set via the same path the live engine
    # uses (set_player_role + raw SQL nudge for death fields).
    await repo.set_player_role(GAME_ID, 1, Role.VILLAGER)
    await repo.set_player_role(GAME_ID, 2, Role.WEREWOLF)
    async with repo._tx() as db:
        await db.execute(
            "UPDATE seats SET alive=0, death_cause=?, death_day=? WHERE game_id=? AND seat_no=2",
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


async def test_export_game_emits_wolf_chat_logs_per_phase(
    fixture_repo: tuple[SqliteRepo, Path], tmp_path: Path
) -> None:
    """Wolf-only night chat is stored fan-out (one row per audience seat)
    in ``logs_private``. The exporter must dedupe to one entry per
    actual utterance and attach it to the matching NIGHT phase.

    Why this matters: viewer renders the wolves' coordination in the
    night timeline. Pre-fix the WOLF_CHAT rows were excluded entirely
    (exporter only read ``logs_public``), so the viewer had no way to
    surface the most informative private signal a post-game review
    needs (which wolf proposed which target, in what order).
    """
    repo, db_path = fixture_repo
    await _seed_minimal_game(repo)

    # Add a second wolf so the fan-out (one row per audience) is
    # exercised — the exporter must collapse N audience rows back to 1.
    seat3 = Seat(
        seat_no=3,
        display_name="Wolf2",
        is_llm=True,
        persona_key="raqio",
        discord_user_id="u3",
    )
    await repo.insert_seat(GAME_ID, seat3)
    await repo.set_player_role(GAME_ID, 3, Role.WEREWOLF)

    # Wolf seat 2's NIGHT_1 utterance, fan-out written to both wolves.
    for audience in (2, 3):
        await repo.insert_log_private(
            LogEntry(
                game_id=GAME_ID,
                day=1,
                phase=Phase.NIGHT,
                kind="WOLF_CHAT",
                actor_seat=2,
                visibility="PRIVATE",
                audience_seat=audience,
                text="セツを噛もう",
                created_at=1_700_000_700,
            )
        )
    # Wolf seat 3's reply.
    for audience in (2, 3):
        await repo.insert_log_private(
            LogEntry(
                game_id=GAME_ID,
                day=1,
                phase=Phase.NIGHT,
                kind="WOLF_CHAT",
                actor_seat=3,
                visibility="PRIVATE",
                audience_seat=audience,
                text="同意。霊媒回避が優先",
                created_at=1_700_000_710,
            )
        )

    out = await export_game(
        game_id=GAME_ID,
        db_path=db_path,
        trace_dir=tmp_path / "no_trace",
        output_dir=tmp_path / "out",
    )
    payload = json.loads(out.read_text(encoding="utf-8"))

    # NIGHT phase appears in the timeline because of the wolf chat
    # rows (no NIGHT_1 actions or public logs were seeded for day 1).
    night1 = next(p for p in payload["phases"] if p["day"] == 1 and p["phase"] == "NIGHT")
    assert len(night1["wolf_chat_logs"]) == 2  # deduped from 4 rows
    first, second = night1["wolf_chat_logs"]
    assert first["actor_seat"] == 2
    assert first["text"] == "セツを噛もう"
    assert first["created_at_ms"] == 1_700_000_700_000
    assert second["actor_seat"] == 3
    assert second["text"] == "同意。霊媒回避が優先"

    # Day-side phases stay empty so nothing leaks across.
    disc = next(p for p in payload["phases"] if p["phase"] == "DAY_DISCUSSION")
    assert disc["wolf_chat_logs"] == []


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
        json.dumps(
            {
                "ts": "2026-04-28T08:00:01+00:00",
                "role": "gameplay",
                "model": "grok-4-1-fast",
                "system_prompt": "s",
                "user_prompt": "u",
                "response": "r",
                "latency_ms": 100,
                "tokens": {"prompt": 5, "completion": 1, "total": 6},
                "phase": "DAY_DISCUSSION",
                "day": 1,
                "actor": "seat=2 persona=setsu",
                "provider": "xai",
                "error": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (game_trace / "voice_stt.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-04-28T08:00:00+00:00",
                "role": "voice_stt",
                "provider": "gemini",
                "model": "gemini-2.0-flash-lite",
                "system_prompt": "s",
                "user_prompt": "[audio]",
                "response": "{}",
                "latency_ms": 800,
                "tokens": None,
                "phase": None,
                "day": None,
                "actor": None,
                "error": None,
            }
        )
        + "\n",
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


async def test_export_game_filters_player_speech_logs(
    fixture_repo: tuple[SqliteRepo, Path], tmp_path: Path
) -> None:
    """PLAYER_SPEECH log rows duplicate speech_events rows; the export omits them.

    DiscussionService.record() inserts both at write time so the live LLM
    context-builder keeps reading PLAYER_SPEECH from logs_public, but the
    viewer should only see one canonical speech entry per utterance.
    """
    repo, db_path = fixture_repo
    await _seed_minimal_game(repo)

    # Add a PLAYER_SPEECH log row that duplicates a npc_generated speech_event.
    await repo.insert_log_public(
        LogEntry(
            game_id=GAME_ID,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            kind="PLAYER_SPEECH",
            actor_seat=2,
            visibility="PUBLIC",
            text="seat 2's npc speech",
            created_at=1_700_000_200,
        )
    )

    out = await export_game(
        game_id=GAME_ID,
        db_path=db_path,
        trace_dir=tmp_path / "no_trace",
        output_dir=tmp_path / "out",
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    all_log_kinds = {log["kind"] for phase in payload["phases"] for log in phase["public_logs"]}
    assert "PLAYER_SPEECH" not in all_log_kinds


async def test_export_game_inlines_arbiter_decisions(
    fixture_repo: tuple[SqliteRepo, Path], tmp_path: Path
) -> None:
    """Arbiter decisions (Master-side `SpeakRequest` dispatches) are joined
    from the three NPC orchestration tables and emitted under the new
    ``arbiter_decisions`` key. The viewer uses this to render the "why this
    NPC, why now" breadcrumb alongside each NPC speech event.
    """
    repo, db_path = fixture_repo
    await _seed_minimal_game(repo)

    # Insert one full request → result → playback chain.
    await repo.insert_npc_speak_request(
        request_id="sr_t1",
        game_id=GAME_ID,
        phase_id=f"{GAME_ID}::day1::DAY_DISCUSSION::1",
        npc_id="npc_setsu",
        seat_no=2,
        logic_packet_id="lp_t1",
        suggested_intent="speak",
        max_chars=80,
        max_duration_ms=12_000,
        priority=0,
        expires_at_ms=1_700_000_300_000,
        created_at_ms=1_700_000_200_000,
        selection_reason="addressed",
        public_state_snapshot={
            "last_addressed_seat": 2,
            "silent_seats": [1, 2],
            "alive_seat_nos": [1, 2],
            "online_npc_seats": [2],
        },
    )
    await repo.insert_npc_speak_result(
        request_id="sr_t1",
        game_id=GAME_ID,
        phase_id=f"{GAME_ID}::day1::DAY_DISCUSSION::1",
        npc_id="npc_setsu",
        status="accepted",
        text="占い師COします",
        used_logic_ids=["co-1-seer"],
        intent="speak",
        estimated_duration_ms=2_500,
        failure_reason=None,
        received_at_ms=1_700_000_205_000,
    )
    await repo.open_npc_playback(
        request_id="sr_t1",
        game_id=GAME_ID,
        phase_id=f"{GAME_ID}::day1::DAY_DISCUSSION::1",
        npc_id="npc_setsu",
        speech_event_id="se_t1",
        authorized_at_ms=1_700_000_205_500,
        playback_deadline_ms=1_700_000_217_500,
    )
    await repo.close_npc_playback(
        "sr_t1",
        finished_at_ms=1_700_000_208_000,
        outcome="success",
        failure_reason=None,
    )

    # And a second request that was rejected before TTS — verifies
    # LEFT JOIN behavior (no playback row, but result row with status).
    await repo.insert_npc_speak_request(
        request_id="sr_t2",
        game_id=GAME_ID,
        phase_id=f"{GAME_ID}::day1::DAY_DISCUSSION::1",
        npc_id="npc_gina",
        seat_no=3,
        logic_packet_id="lp_t2",
        suggested_intent="speak",
        max_chars=80,
        max_duration_ms=12_000,
        priority=0,
        expires_at_ms=1_700_000_320_000,
        created_at_ms=1_700_000_220_000,
        selection_reason="silent_rotation",
        public_state_snapshot={"silent_seats": [3]},
    )
    await repo.insert_npc_speak_result(
        request_id="sr_t2",
        game_id=GAME_ID,
        phase_id=f"{GAME_ID}::day1::DAY_DISCUSSION::1",
        npc_id="npc_gina",
        status="rejected",
        text=None,
        used_logic_ids=None,
        intent=None,
        estimated_duration_ms=None,
        failure_reason="stale_phase",
        received_at_ms=1_700_000_225_000,
    )

    out = await export_game(
        game_id=GAME_ID,
        db_path=db_path,
        trace_dir=tmp_path / "no_trace",
        output_dir=tmp_path / "out",
    )
    payload = json.loads(out.read_text(encoding="utf-8"))

    decisions = payload["arbiter_decisions"]
    assert len(decisions) == 2

    # Chronological order — sr_t1 first, sr_t2 second.
    d1, d2 = decisions
    assert d1["request_id"] == "sr_t1"
    assert d1["selection_reason"] == "addressed"
    assert d1["public_state_snapshot"]["last_addressed_seat"] == 2
    assert d1["result_status"] == "accepted"
    assert d1["result_text"] == "占い師COします"
    assert d1["playback_outcome"] == "success"
    assert d1["playback_finished_at_ms"] == 1_700_000_208_000

    # Rejected request: result populated but playback fields all None.
    assert d2["request_id"] == "sr_t2"
    assert d2["selection_reason"] == "silent_rotation"
    assert d2["result_status"] == "rejected"
    assert d2["result_failure_reason"] == "stale_phase"
    assert d2["playback_outcome"] is None
    assert d2["playback_finished_at_ms"] is None


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


async def test_export_game_filename_uses_timestamp_prefix(
    fixture_repo: tuple[SqliteRepo, Path], tmp_path: Path
) -> None:
    """Filename is derived from the game's created_at (local time), not the
    random game_id, so the viewer dir lists games sortably by play time."""
    repo, db_path = fixture_repo
    await _seed_minimal_game(repo)

    out = await export_game(
        game_id=GAME_ID,
        db_path=db_path,
        trace_dir=tmp_path / "no_trace",
        output_dir=tmp_path / "out",
    )

    # Filename must NOT be {game_id}.json.
    assert out.name != f"{GAME_ID}.json"
    # Filename must be derived from created_at_ms = 1_700_000_000_000 →
    # 2023-11-15 in UTC. Local-time conversion may shift the date but the
    # YYYY-MM-DD_HH-MM-SS shape is invariant.
    import re

    assert re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:_\d+)?\.json$", out.name), (
        f"unexpected filename shape: {out.name}"
    )


async def test_export_game_filename_disambiguates_same_second_collision(
    fixture_repo: tuple[SqliteRepo, Path], tmp_path: Path
) -> None:
    """If a different game already wrote a same-second file, append `_<n>`.
    Re-exporting the same game (same id) must overwrite, not collide."""
    repo, db_path = fixture_repo
    await _seed_minimal_game(repo)
    out_dir = tmp_path / "out"

    first = await export_game(
        game_id=GAME_ID,
        db_path=db_path,
        trace_dir=tmp_path / "no_trace",
        output_dir=out_dir,
    )
    # Re-export same game — should overwrite, not produce a `_1` sibling.
    second = await export_game(
        game_id=GAME_ID,
        db_path=db_path,
        trace_dir=tmp_path / "no_trace",
        output_dir=out_dir,
    )
    assert first == second
    assert len(list(out_dir.glob("*.json"))) == 1

    # Now drop a fake same-second file from a different "game id" and
    # re-export — should disambiguate with `_1` suffix.
    other = out_dir / first.name
    other.write_text(
        json.dumps({"game": {"id": "different-game-id"}}),
        encoding="utf-8",
    )
    third = await export_game(
        game_id=GAME_ID,
        db_path=db_path,
        trace_dir=tmp_path / "no_trace",
        output_dir=out_dir,
    )
    assert third != first
    assert third.name.endswith("_1.json")


async def test_export_game_phases_chronological_with_correctly_tagged_logs(
    fixture_repo: tuple[SqliteRepo, Path],
    tmp_path: Path,
) -> None:
    """A post-fix game whose MORNING + day-start PHASE_CHANGE logs are
    already tagged with the right ``day`` must export phases in
    chronological order and not duplicate buckets one day off.

    Reproduces the bug fixed by replacing ``_rebucket_morning_logs``
    with ``_retag_morning_logs_from_text``: the old helper bumped every
    matching row's day by +1 unconditionally, so a correctly-tagged
    game emitted phantom (day+1, DAY_DISCUSSION) buckets containing
    only the morning logs while the actual same-day speeches stayed in
    the original bucket.
    """
    repo, db_path = fixture_repo
    base = 1_777_700_000  # epoch s, arbitrary
    game = Game(
        id="g_phase_order",
        guild_id="g",
        host_user_id="h",
        phase=Phase.GAME_OVER,
        day_number=2,
        deadline_epoch=None,
        main_text_channel_id="c",
        main_vc_channel_id="v",
        heaven_channel_id=None,
        wolves_channel_id=None,
        created_at=base,
        ended_at=base + 1000,
        force_skip_pending=False,
        discussion_mode="rounds",
    )
    await repo.create_game(game)
    await repo.insert_seat(
        game.id,
        Seat(
            seat_no=1,
            display_name="A",
            discord_user_id="u1",
            is_llm=False,
            persona_key=None,
        ),
    )

    async def _log(*, day: int, phase: Phase, kind: str, text: str, t: int) -> None:
        await repo.insert_log_public(
            LogEntry(
                game_id=game.id,
                day=day,
                phase=phase,
                kind=kind,
                actor_seat=None,
                visibility="PUBLIC",
                text=text,
                created_at=t,
            )
        )

    # day-1 morning + first speech, vote, then day-2 morning + speech.
    await _log(
        day=1,
        phase=Phase.DAY_DISCUSSION,
        kind="PHASE_CHANGE",
        text="夜が明けました。1 日目の議論を開始します。",
        t=base + 10,
    )
    await _log(
        day=1,
        phase=Phase.DAY_VOTE,
        kind="PHASE_CHANGE",
        text="議論時間終了。投票フェイズを開始します。",
        t=base + 100,
    )
    await _log(
        day=2, phase=Phase.DAY_DISCUSSION, kind="MORNING", text="平和な朝です。", t=base + 200
    )
    await _log(
        day=2,
        phase=Phase.DAY_DISCUSSION,
        kind="PHASE_CHANGE",
        text="2 日目の議論を開始します。",
        t=base + 200,
    )
    await _log(
        day=2,
        phase=Phase.DAY_VOTE,
        kind="PHASE_CHANGE",
        text="議論時間終了。投票フェイズを開始します。",
        t=base + 300,
    )

    out_path = await export_game(
        game_id=game.id,
        db_path=db_path,
        trace_dir=tmp_path / "no_trace",
        output_dir=tmp_path / "out",
    )
    payload = json.loads(out_path.read_text())
    # No phantom day-3 bucket from morning rebumping.
    assert {(p["day"], p["phase"]) for p in payload["phases"]} == {
        (1, "DAY_DISCUSSION"),
        (1, "DAY_VOTE"),
        (2, "DAY_DISCUSSION"),
        (2, "DAY_VOTE"),
    }
    # And they appear in chronological order.
    starts = [p["started_at_ms"] for p in payload["phases"]]
    assert starts == sorted(starts)


async def test_export_game_retags_legacy_morning_logs_from_text(
    fixture_repo: tuple[SqliteRepo, Path],
    tmp_path: Path,
) -> None:
    """A pre-fix game whose MORNING + day-start PHASE_CHANGE logs are
    tagged with the *prior* day must still produce a correctly-ordered
    timeline. The retagger reads the day number out of the PHASE_CHANGE
    text ("2 日目の議論を開始") and uses it as the source of truth.
    """
    repo, db_path = fixture_repo
    base = 1_777_700_000
    game = Game(
        id="g_phase_order_legacy",
        guild_id="g2",
        host_user_id="h",
        phase=Phase.GAME_OVER,
        day_number=2,
        deadline_epoch=None,
        main_text_channel_id="c",
        main_vc_channel_id="v",
        heaven_channel_id=None,
        wolves_channel_id=None,
        created_at=base,
        ended_at=base + 1000,
        force_skip_pending=False,
        discussion_mode="rounds",
    )
    await repo.create_game(game)
    await repo.insert_seat(
        game.id,
        Seat(
            seat_no=1,
            display_name="A",
            discord_user_id="u1",
            is_llm=False,
            persona_key=None,
        ),
    )

    async def _log(*, day: int, phase: Phase, kind: str, text: str, t: int) -> None:
        await repo.insert_log_public(
            LogEntry(
                game_id=game.id,
                day=day,
                phase=phase,
                kind=kind,
                actor_seat=None,
                visibility="PUBLIC",
                text=text,
                created_at=t,
            )
        )

    # Legacy tagging: NIGHT_0 → DAY_1 morning was emitted with day=0,
    # NIGHT_1 → DAY_2 morning was emitted with day=1. Text carries the
    # correct N (1, 2) so the retagger can fix it on read.
    await _log(
        day=0,
        phase=Phase.DAY_DISCUSSION,
        kind="PHASE_CHANGE",
        text="夜が明けました。1 日目の議論を開始します。",
        t=base + 10,
    )
    await _log(
        day=1,
        phase=Phase.DAY_VOTE,
        kind="PHASE_CHANGE",
        text="議論時間終了。投票フェイズを開始します。",
        t=base + 100,
    )
    await _log(
        day=1, phase=Phase.DAY_DISCUSSION, kind="MORNING", text="平和な朝です。", t=base + 200
    )
    await _log(
        day=1,
        phase=Phase.DAY_DISCUSSION,
        kind="PHASE_CHANGE",
        text="2 日目の議論を開始します。",
        t=base + 200,
    )

    out_path = await export_game(
        game_id=game.id,
        db_path=db_path,
        trace_dir=tmp_path / "no_trace",
        output_dir=tmp_path / "out",
    )
    payload = json.loads(out_path.read_text())
    pairs = {(p["day"], p["phase"]) for p in payload["phases"]}
    # The 2 日目 PHASE_CHANGE + MORNING were tagged day=1 in DB but
    # belong to day=2. After retagging they land in (2, DAY_DISCUSSION),
    # not (1, DAY_DISCUSSION).
    assert (2, "DAY_DISCUSSION") in pairs
    # And no leftover-from-misbucket (1, DAY_DISCUSSION) with only the
    # PHASE_CHANGE row from day-1's start logs.
    day1_disc = next(
        p for p in payload["phases"] if p["day"] == 1 and p["phase"] == "DAY_DISCUSSION"
    )
    # day-1's discussion only contains the "1 日目の議論を開始" row,
    # which the retagger leaves as day=1 (its text says so).
    assert len(day1_disc["public_logs"]) == 1
    assert "1 日目の議論を開始" in day1_disc["public_logs"][0]["text"]
