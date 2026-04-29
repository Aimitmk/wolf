"""Bundle 5: master arbitration + recovery — protocol-level coverage.

Verifies the SpeakArbiter logic without standing up a real Master. Each
test exercises one branch of the arbitration flow:

- Successful dispatch + accepted SpeakResult → SpeechEvent + PlaybackAuthorized.
- Stale phase / expired request / over-length text → rejection paths.
- Serial-speech gate blocks while a playback is open and while a human is
  speaking.
- Offline NPC is skipped without sending a SpeakRequest.
- Recovery sweep closes in-flight rows with master_restart.
- Master restart rebuilds PublicDiscussionState from speech_events.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from wolfbot.domain.discussion import (
    PublicDiscussionState,
    SpeechSource,
    make_phase_id,
)
from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Seat
from wolfbot.domain.ws_messages import (
    PlaybackAuthorized,
    PlaybackFinished,
    PlaybackRejected,
    SpeakResult,
)
from wolfbot.master.logic_service import build_logic_packet
from wolfbot.master.npc_registry import InMemoryNpcRegistry
from wolfbot.master.speak_arbiter import SpeakArbiter, SpeakArbiterConfig
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discussion_service import (
    DiscussionService,
    SqliteSpeechEventStore,
    make_phase_baseline,
)


def _captured_send(buf: list[str]) -> Callable[[str], Awaitable[None]]:
    async def send(msg: str) -> None:
        buf.append(msg)

    return send


async def _seed_game(repo: SqliteRepo) -> tuple[Game, list[Seat]]:
    g = Game(
        id="rv1",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(g)
    seats = [
        Seat(seat_no=1, display_name="Alice", discord_user_id="u1", is_llm=False, persona_key=None),
        Seat(
            seat_no=2,
            display_name="セツ",
            discord_user_id=None,
            is_llm=True,
            persona_key="setsu",
        ),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 2, Role.SEER)
    return g, seats


def _seed_state(game_id: str, day: int = 1) -> PublicDiscussionState:
    phase_id = make_phase_id(game_id, day, Phase.DAY_DISCUSSION)
    return PublicDiscussionState(
        game_id=game_id,
        phase_id=phase_id,
        day=day,
        alive_seat_nos=frozenset({1, 2}),
        silent_seats=frozenset({1, 2}),
    )


async def test_successful_dispatch_emits_logic_packet_and_speak_request(
    repo: SqliteRepo,
) -> None:
    game, _seats = await _seed_game(repo)
    registry = InMemoryNpcRegistry()
    npc_buf: list[str] = []
    registry.register(
        npc_id="npc_p2",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_captured_send(npc_buf),
        now_ms=1000, persona_key="setsu")
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 1500,
    )
    state = _seed_state(game.id)

    request, reason = await arb.dispatch_request(
        state=state,
        candidate_npc_id="npc_p2",
        seat_no=2,
        game_id=game.id,
    )
    assert reason is None and request is not None
    # Two messages on the back-channel: LogicPacket then SpeakRequest.
    assert len(npc_buf) == 2
    assert '"type":"logic_packet"' in npc_buf[0]
    assert '"type":"speak_request"' in npc_buf[1]


async def test_dispatch_records_request_in_audit_table(repo: SqliteRepo) -> None:
    game, _ = await _seed_game(repo)
    registry = InMemoryNpcRegistry()
    npc_buf: list[str] = []
    registry.register(
        npc_id="npc_p2",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_captured_send(npc_buf),
        now_ms=1000, persona_key="setsu")
    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 1500)
    state = _seed_state(game.id)
    request, _ = await arb.dispatch_request(
        state=state, candidate_npc_id="npc_p2", seat_no=2, game_id=game.id
    )
    assert request is not None
    rows = await repo.load_open_npc_speak_requests(game.id)
    assert any(r["request_id"] == request.request_id for r in rows)


async def test_offline_candidate_is_skipped(repo: SqliteRepo) -> None:
    game, _ = await _seed_game(repo)
    registry = InMemoryNpcRegistry()
    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 1)
    state = _seed_state(game.id)
    req, reason = await arb.dispatch_request(
        state=state, candidate_npc_id="never_registered", seat_no=2, game_id=game.id
    )
    assert req is None and reason == "npc_offline"


async def test_human_speaking_blocks_dispatch(repo: SqliteRepo) -> None:
    game, _ = await _seed_game(repo)
    registry = InMemoryNpcRegistry()
    npc_buf: list[str] = []
    registry.register(
        npc_id="npc_p2",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_captured_send(npc_buf),
        now_ms=1000, persona_key="setsu")
    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 1500)
    arb.mark_human_speaking("seg-1")
    req, reason = await arb.dispatch_request(
        state=_seed_state(game.id), candidate_npc_id="npc_p2", seat_no=2, game_id=game.id
    )
    assert req is None and reason == "human_currently_speaking"
    arb.clear_human_speaking("seg-1")
    req, reason = await arb.dispatch_request(
        state=_seed_state(game.id), candidate_npc_id="npc_p2", seat_no=2, game_id=game.id
    )
    assert req is not None and reason is None


async def test_dispatch_attaches_context_to_logic_packet_and_speak_request(
    repo: SqliteRepo,
) -> None:
    """SpeakArbiter loads recent speeches, alive/dead lists, and the seat's
    role+strategy and forwards them on the WS payload so the NPC's prompt
    builder has the same context that rounds-mode build_user_context does.
    """
    from wolfbot.domain.discussion import (
        SpeakerKind,
        SpeechEvent,
        make_phase_id,
    )
    from wolfbot.domain.ws_messages import LogicPacket, SpeakRequest
    from wolfbot.services.discussion_service import (
        new_event_id,
    )
    from wolfbot.services.discussion_service import (
        now_ms as discussion_now_ms,
    )

    game, _seats = await _seed_game(repo)
    registry = InMemoryNpcRegistry()
    npc_buf: list[str] = []
    registry.register(
        npc_id="npc_p2",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_captured_send(npc_buf),
        now_ms=1000,
        persona_key="setsu",
    )
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)

    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)
    await discussion.record(
        make_phase_baseline(
            game_id=game.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2],
        )
    )
    await discussion.record(
        SpeechEvent(
            event_id=new_event_id(),
            game_id=game.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            source=SpeechSource.TEXT,
            speaker_kind=SpeakerKind.HUMAN,
            speaker_seat=1,
            text="占いの結果が気になる",
            created_at_ms=discussion_now_ms(),
        )
    )

    arb = SpeakArbiter(
        repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 1500
    )
    state = _seed_state(game.id)
    request, reason = await arb.dispatch_request(
        state=state, candidate_npc_id="npc_p2", seat_no=2, game_id=game.id
    )
    assert reason is None and request is not None

    packet = LogicPacket.model_validate_json(npc_buf[0])
    speak = SpeakRequest.model_validate_json(npc_buf[1])

    # Recent speech surfaces with the speaker's display name attached.
    assert len(packet.recent_speeches) == 1
    assert packet.recent_speeches[0].seat_no == 1
    assert packet.recent_speeches[0].display_name == "Alice"
    assert packet.recent_speeches[0].source == "text"
    assert "占いの結果が気になる" in packet.recent_speeches[0].text

    # Role + role_strategy of seat 2 (SEER per _seed_game) is forwarded.
    assert speak.role == "SEER"
    assert speak.role_strategy is not None and len(speak.role_strategy) > 0

    # Both seats alive at this point — alive_seats has both, dead_seats empty.
    assert speak.alive_seats == ((1, "Alice"), (2, "セツ"))
    assert speak.dead_seats == ()


async def test_speak_result_accepted_emits_authorized_and_writes_speech_event(
    repo: SqliteRepo,
) -> None:
    game, _ = await _seed_game(repo)
    registry = InMemoryNpcRegistry()
    npc_buf: list[str] = []
    registry.register(
        npc_id="npc_p2",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_captured_send(npc_buf),
        now_ms=1000, persona_key="setsu")
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    arb = SpeakArbiter(repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 1500)
    state = _seed_state(game.id)
    req, _ = await arb.dispatch_request(
        state=state, candidate_npc_id="npc_p2", seat_no=2, game_id=game.id
    )
    assert req is not None
    npc_buf.clear()

    result = SpeakResult(
        ts=1600,
        trace_id="t",
        request_id=req.request_id,
        npc_id="npc_p2",
        phase_id=req.phase_id,
        status="accepted",
        text="うーん怪しいかも",
    )
    ok, reason = await arb.handle_speak_result(
        result, current_phase_id=req.phase_id, day=1, phase=Phase.DAY_DISCUSSION
    )
    assert ok and reason is None
    assert any('"type":"playback_authorized"' in m for m in npc_buf)
    auth = next(
        PlaybackAuthorized.model_validate_json(m) for m in npc_buf if '"playback_authorized"' in m
    )
    assert auth.npc_id == "npc_p2"
    rows = await store.load_phase(game.id, req.phase_id)
    assert any(
        r.source == SpeechSource.NPC_GENERATED and r.text == "うーん怪しいかも" for r in rows
    )


async def test_speak_result_over_length_rejected(repo: SqliteRepo) -> None:
    game, _ = await _seed_game(repo)
    registry = InMemoryNpcRegistry()
    npc_buf: list[str] = []
    registry.register(
        npc_id="npc_p2",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_captured_send(npc_buf),
        now_ms=1000, persona_key="setsu")
    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        config=SpeakArbiterConfig(max_chars_reactive=10),
        now_ms=lambda: 1500,
    )
    state = _seed_state(game.id)
    req, _ = await arb.dispatch_request(
        state=state, candidate_npc_id="npc_p2", seat_no=2, game_id=game.id
    )
    assert req is not None
    npc_buf.clear()
    result = SpeakResult(
        ts=1600,
        trace_id="t",
        request_id=req.request_id,
        npc_id="npc_p2",
        phase_id=req.phase_id,
        status="accepted",
        text="this exceeds the cap definitely",
    )
    ok, reason = await arb.handle_speak_result(
        result, current_phase_id=req.phase_id, day=1, phase=Phase.DAY_DISCUSSION
    )
    assert not ok and reason == "utterance_too_long"
    rejection = next(
        PlaybackRejected.model_validate_json(m) for m in npc_buf if '"playback_rejected"' in m
    )
    assert rejection.failure_reason == "utterance_too_long"


async def test_speak_result_stale_phase_rejected(repo: SqliteRepo) -> None:
    game, _ = await _seed_game(repo)
    registry = InMemoryNpcRegistry()
    npc_buf: list[str] = []
    registry.register(
        npc_id="npc_p2",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_captured_send(npc_buf),
        now_ms=1000, persona_key="setsu")
    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 1500)
    state = _seed_state(game.id)
    req, _ = await arb.dispatch_request(
        state=state, candidate_npc_id="npc_p2", seat_no=2, game_id=game.id
    )
    assert req is not None
    npc_buf.clear()
    result = SpeakResult(
        ts=1600,
        trace_id="t",
        request_id=req.request_id,
        npc_id="npc_p2",
        phase_id=req.phase_id,  # stays the same on the result
        status="accepted",
        text="ok",
    )
    ok, reason = await arb.handle_speak_result(
        result,
        current_phase_id="some-other-phase",  # arbiter sees a different current phase
        day=1,
        phase=Phase.DAY_DISCUSSION,
    )
    assert not ok and reason == "stale_phase"


async def test_serial_speech_blocks_after_authorize_until_finished(
    repo: SqliteRepo,
) -> None:
    game, _ = await _seed_game(repo)
    registry = InMemoryNpcRegistry()
    npc_buf: list[str] = []
    registry.register(
        npc_id="npc_p2",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_captured_send(npc_buf),
        now_ms=1000, persona_key="setsu")
    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 1500)
    state = _seed_state(game.id)
    req, _ = await arb.dispatch_request(
        state=state, candidate_npc_id="npc_p2", seat_no=2, game_id=game.id
    )
    assert req is not None
    await arb.handle_speak_result(
        SpeakResult(
            ts=1600,
            trace_id="t",
            request_id=req.request_id,
            npc_id="npc_p2",
            phase_id=req.phase_id,
            status="accepted",
            text="まあね",
        ),
        current_phase_id=req.phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
    )
    # Now block: next dispatch should fail with queue_busy.
    req2, reason = await arb.dispatch_request(
        state=state, candidate_npc_id="npc_p2", seat_no=2, game_id=game.id
    )
    assert req2 is None and reason == "queue_busy"
    # Close the playback — the gate should release.
    await arb.handle_playback_finished(
        PlaybackFinished(
            ts=1700,
            trace_id="t",
            request_id=req.request_id,
            npc_id="npc_p2",
            started_at_ms=1600,
            finished_at_ms=1700,
        )
    )
    req3, reason = await arb.dispatch_request(
        state=state, candidate_npc_id="npc_p2", seat_no=2, game_id=game.id
    )
    assert req3 is not None and reason is None


async def test_recovery_sweep_marks_in_flight_rows_master_restart(
    repo: SqliteRepo,
) -> None:
    game, _ = await _seed_game(repo)
    registry = InMemoryNpcRegistry()
    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 9999)
    # Manually seed one open request and one open playback (without going
    # through dispatch — Master crashed mid-flight).
    await repo.insert_npc_speak_request(
        request_id="open-req-1",
        game_id=game.id,
        phase_id="ph",
        npc_id="npc_p2",
        seat_no=2,
        logic_packet_id="lp",
        suggested_intent="x",
        max_chars=80,
        max_duration_ms=1000,
        priority=0,
        expires_at_ms=10000,
        created_at_ms=8000,
    )
    await repo.open_npc_playback(
        request_id="open-pb-1",
        game_id=game.id,
        phase_id="ph",
        npc_id="npc_p2",
        speech_event_id="ev1",
        authorized_at_ms=8000,
        playback_deadline_ms=12000,
    )
    await arb.reactive_voice_recovery_sweep(game.id)
    open_reqs = await repo.load_open_npc_speak_requests(game.id)
    open_play = await repo.load_open_npc_playback(game.id)
    assert open_reqs == []
    assert open_play == []


async def test_rebuild_public_state_from_master_restart(repo: SqliteRepo) -> None:
    game, _ = await _seed_game(repo)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)
    sentinel = make_phase_baseline(
        game_id=game.id,
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=[1, 2],
        created_at_ms=1,
    )
    await store.insert(sentinel)
    # Simulate a recovered SpeakArbiter on a fresh process.
    arb = SpeakArbiter(
        repo=repo,
        registry=InMemoryNpcRegistry(),
        discussion=discussion,
        now_ms=lambda: 5,
    )
    state = await arb.rebuild_public_state(game_id=game.id, day=1, phase=Phase.DAY_DISCUSSION)
    assert state is not None
    assert state.alive_seat_nos == frozenset({1, 2})


async def test_try_dispatch_next_prefers_silent_seat_over_lowest(
    repo: SqliteRepo,
) -> None:
    """In pure-NPC games the only rotation signal is `silent_seats`. Without
    silent-first ordering, `try_dispatch_next` would always pick the lowest
    `assigned_seat` and the NPC at seat 2 would monopolize. The picker must
    prefer NPCs whose seats are still in `silent_seats`.
    """
    g = Game(
        id="rv-pick",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=10**12,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    seats = [
        Seat(seat_no=1, display_name="Alice", discord_user_id="u1", is_llm=False, persona_key=None),
        Seat(seat_no=2, display_name="セツ", discord_user_id=None, is_llm=True, persona_key="setsu"),
        Seat(seat_no=3, display_name="ジーナ", discord_user_id=None, is_llm=True, persona_key="gina"),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 2, Role.SEER)
    await repo.set_player_role(g.id, 3, Role.VILLAGER)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    # Seed phase baseline so PublicDiscussionState has alive_seat_nos {1,2,3}.
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3],
            created_at_ms=1,
        )
    )
    # Seat 2 has already spoken — silent_seats is now {1, 3}. Seat 1 is human
    # (no assigned_seat in registry), so the only silent NPC seat is 3.
    from wolfbot.services.discussion_service import make_npc_generated_event

    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=2,
            text="seat 2 already spoke",
            created_at_ms=2,
        )
    )

    registry = InMemoryNpcRegistry()
    buf2: list[str] = []
    buf3: list[str] = []
    registry.register(
        npc_id="npc_setsu",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_captured_send(buf2),
        now_ms=1000,
        persona_key="setsu",
    )
    registry.register(
        npc_id="npc_gina",
        discord_bot_user_id="bot3",
        supported_voices=(),
        version="1",
        send=_captured_send(buf3),
        now_ms=1000,
        persona_key="gina",
    )
    registry.assign("npc_setsu", seat=2, game_id=g.id, phase_id=phase_id)
    registry.assign("npc_gina", seat=3, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 1500,
    )
    await arb.try_dispatch_next(g.id)

    # Without the silent-first fix, the lowest-seat NPC (seat 2) would have
    # received the SpeakRequest. With the fix, only seat 3 (the silent NPC)
    # gets it.
    assert buf3, "silent NPC at seat 3 must be picked"
    assert not buf2, "non-silent NPC at seat 2 must not be picked"


async def _fetch_selection_reason(
    repo: SqliteRepo, game_id: str
) -> tuple[str | None, str | None]:
    """Pull (seat_no_repr, reason) for the most recent dispatched request.

    Used by the selection_reason classification tests; the column is
    only populated by the `try_dispatch_next` path, so a test that uses
    the lower-level `dispatch_request` directly will see ``None``.
    """
    async with repo._conn.execute(  # type: ignore[attr-defined]
        """
        SELECT seat_no, selection_reason, public_state_snapshot_json
          FROM npc_speak_requests
         WHERE game_id = ?
         ORDER BY created_at_ms DESC
         LIMIT 1
        """,
        (game_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return (None, None)
    return (str(row[0]), row[1])


async def test_try_dispatch_next_records_selection_reason_addressed(
    repo: SqliteRepo,
) -> None:
    """When state.last_addressed_seat matches an online NPC seat, the
    arbiter records ``selection_reason='addressed'`` and snapshots the
    public-state context (silent_seats, alive_seat_nos, online_npc_seats)
    onto the persisted row so the viewer can render the "why".
    """
    g = Game(
        id="rv-reason-addr",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=10**12,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    seats = [
        Seat(seat_no=1, display_name="Alice", discord_user_id="u1", is_llm=False, persona_key=None),
        Seat(seat_no=2, display_name="セツ", discord_user_id=None, is_llm=True, persona_key="setsu"),
        Seat(seat_no=3, display_name="ジーナ", discord_user_id=None, is_llm=True, persona_key="gina"),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 2, Role.SEER)
    await repo.set_player_role(g.id, 3, Role.VILLAGER)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3],
            created_at_ms=1,
        )
    )
    # Human at seat 1 addresses NPC at seat 3 — this should override
    # silent-rotation ordering (seat 2 has lower number but isn't addressed).
    from wolfbot.services.discussion_service import make_voice_stt_event

    await store.insert(
        make_voice_stt_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=1,
            text="ジーナどう思う？",
            stt_confidence=0.9,
            audio_start_ms=10,
            audio_end_ms=20,
            addressed_seat_no=3,
            created_at_ms=2,
        )
    )

    registry = InMemoryNpcRegistry()
    buf2: list[str] = []
    buf3: list[str] = []
    registry.register(
        npc_id="npc_setsu",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_captured_send(buf2),
        now_ms=1000,
        persona_key="setsu",
    )
    registry.register(
        npc_id="npc_gina",
        discord_bot_user_id="bot3",
        supported_voices=(),
        version="1",
        send=_captured_send(buf3),
        now_ms=1000,
        persona_key="gina",
    )
    registry.assign("npc_setsu", seat=2, game_id=g.id, phase_id=phase_id)
    registry.assign("npc_gina", seat=3, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 1500,
    )
    await arb.try_dispatch_next(g.id)

    assert buf3, "addressed NPC at seat 3 must receive the SpeakRequest"
    seat_repr, reason = await _fetch_selection_reason(repo, g.id)
    assert seat_repr == "3"
    assert reason == "addressed"


async def test_try_dispatch_next_records_selection_reason_silent_rotation(
    repo: SqliteRepo,
) -> None:
    """No addressed seat → silent NPC wins; reason='silent_rotation'."""
    g = Game(
        id="rv-reason-silent",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=10**12,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    seats = [
        Seat(seat_no=1, display_name="Alice", discord_user_id="u1", is_llm=False, persona_key=None),
        Seat(seat_no=2, display_name="セツ", discord_user_id=None, is_llm=True, persona_key="setsu"),
        Seat(seat_no=3, display_name="ジーナ", discord_user_id=None, is_llm=True, persona_key="gina"),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 2, Role.SEER)
    await repo.set_player_role(g.id, 3, Role.VILLAGER)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3],
            created_at_ms=1,
        )
    )
    # Seat 2 already spoke. silent_seats={1,3}; seat 1 is human (no NPC),
    # so the only silent NPC is seat 3 → reason should be silent_rotation.
    from wolfbot.services.discussion_service import make_npc_generated_event

    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=2,
            text="seat 2 already spoke",
            created_at_ms=2,
        )
    )

    registry = InMemoryNpcRegistry()
    buf2: list[str] = []
    buf3: list[str] = []
    registry.register(
        npc_id="npc_setsu",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_captured_send(buf2),
        now_ms=1000,
        persona_key="setsu",
    )
    registry.register(
        npc_id="npc_gina",
        discord_bot_user_id="bot3",
        supported_voices=(),
        version="1",
        send=_captured_send(buf3),
        now_ms=1000,
        persona_key="gina",
    )
    registry.assign("npc_setsu", seat=2, game_id=g.id, phase_id=phase_id)
    registry.assign("npc_gina", seat=3, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 1500,
    )
    await arb.try_dispatch_next(g.id)

    seat_repr, reason = await _fetch_selection_reason(repo, g.id)
    assert seat_repr == "3"
    assert reason == "silent_rotation"


async def test_try_dispatch_next_avoids_immediate_repeat_after_first_round(
    repo: SqliteRepo,
) -> None:
    """Once `silent_seats` is empty (every alive NPC spoke once), the
    arbiter must not re-pick the most recent speaker. Without this guard
    the lowest-seat NPC monopolizes the rest of the phase — observed in
    the wild where seat 1 (Raqio) spoke 8 times in a row after the
    rotation.
    """
    g = Game(
        id="rv-lru",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=10**12,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    seats = [
        Seat(seat_no=1, display_name="🦋ラキオ", discord_user_id=None,
             is_llm=True, persona_key="raqio"),
        Seat(seat_no=2, display_name="🌙セツ", discord_user_id=None,
             is_llm=True, persona_key="setsu"),
        Seat(seat_no=3, display_name="🟣ジナ", discord_user_id=None,
             is_llm=True, persona_key="gina"),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 2, Role.SEER)
    await repo.set_player_role(g.id, 3, Role.VILLAGER)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id, phase_id=phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3], created_at_ms=1,
        )
    )
    # Simulate a complete first round: every alive NPC spoke once. Last
    # speaker is seat 1 (Raqio). silent_seats becomes empty.
    from wolfbot.services.discussion_service import make_npc_generated_event

    for ts, seat_no, text in (
        (10, 2, "セツ first"),
        (20, 3, "ジナ first"),
        (30, 1, "ラキオ first"),  # ラキオ is the most recent speaker
    ):
        await store.insert(
            make_npc_generated_event(
                game_id=g.id, phase_id=phase_id, day=1,
                phase=Phase.DAY_DISCUSSION,
                speaker_seat=seat_no, text=text, created_at_ms=ts,
            )
        )

    registry = InMemoryNpcRegistry()
    bufs: dict[str, list[str]] = {"raqio": [], "setsu": [], "gina": []}
    for npc_id, persona, seat_no in (
        ("npc_raqio", "raqio", 1),
        ("npc_setsu", "setsu", 2),
        ("npc_gina", "gina", 3),
    ):
        registry.register(
            npc_id=npc_id, discord_bot_user_id=f"bot_{persona}",
            supported_voices=(), version="1",
            send=_captured_send(bufs[persona]),
            now_ms=1000, persona_key=persona,
        )
        registry.assign(npc_id, seat=seat_no, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo, registry=registry, discussion=discussion,
        now_ms=lambda: 2000,
    )
    await arb.try_dispatch_next(g.id)

    assert not bufs["raqio"], (
        "ラキオ was the immediate previous speaker — must NOT be re-picked"
    )
    # Seat 2 (Setsu) wins by lowest seat among non-last-speakers.
    assert bufs["setsu"], "expected next pick to land on seat 2 Setsu"
    assert not bufs["gina"]


async def test_pair_volley_demotion_fires_after_4_low_info_speeches(
    repo: SqliteRepo,
) -> None:
    """A→B→A→B with no CO declared = 4 low-info speeches between 2 seats.
    `_compute_demoted_seats` must mark BOTH seats so the next arbiter
    pick picks a 3rd NPC (e.g., 席3 ジナ) instead of one of the two stuck
    in the volley. Reproduces the production loop where ラキオ ↔ ジョナス
    spoke alternately for the entire phase.
    """
    from wolfbot.master.speak_arbiter import _compute_demoted_seats

    # 4-event window: 1, 2, 1, 2 (no info)
    summary = ((1, False), (2, False), (1, False), (2, False))
    demoted = _compute_demoted_seats(summary)
    assert demoted == frozenset({1, 2})


async def test_pair_volley_resets_when_co_declared() -> None:
    """A *fresh* CO declaration in the window flips ``has_info=True`` for
    that event → the gate must NOT fire. Otherwise legitimate dramatic
    moments (e.g. seer CO in the middle of a heated exchange) would be
    punished. Note: only the first CO per (seat, role) sets has_info —
    the fold dedups so re-declaring an existing CO doesn't bypass.
    """
    from wolfbot.master.speak_arbiter import _compute_demoted_seats

    summary = ((1, False), (2, False), (1, True), (2, False))
    assert _compute_demoted_seats(summary) == frozenset()


async def test_repeated_co_from_same_seat_does_not_bypass_gate(
    repo: SqliteRepo,
) -> None:
    """The day-1 ジョナス↔ラキオ ping-pong escaped the gate because
    Raqio kept emitting the same `co_declaration='seer'` flag on every
    speech, making each event look like new info. Dedup CO at fold
    time so the gate fires when the only "info" is a repeated CO.
    """
    g = Game(
        id="rv-co-repeat",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=10**12,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    seats = [
        Seat(seat_no=1, display_name="Alice", discord_user_id="u1",
             is_llm=False, persona_key=None),
        Seat(seat_no=2, display_name="🎩ジョナス", discord_user_id=None,
             is_llm=True, persona_key="jonas"),
        Seat(seat_no=3, display_name="🦋ラキオ", discord_user_id=None,
             is_llm=True, persona_key="raqio"),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 2, Role.WEREWOLF)
    await repo.set_player_role(g.id, 3, Role.SEER)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    await store.insert(
        make_phase_baseline(
            game_id=g.id, phase_id=phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3], created_at_ms=1,
        )
    )
    from wolfbot.services.discussion_service import (
        make_npc_generated_event,
        rebuild_public_state_from_events,
    )
    # First CO at ts=5 is genuine; the next 4 events form the volley
    # window. Raqio re-emits `co_declaration='seer'` on every reply but
    # the dedup makes those repeats has_info=False, so the window's last
    # 4 entries are all (seat, False) → gate fires.
    events_seq = [
        (5, 3, "seer"),        # first CO (outside the last-4 window)
        (10, 2, None),         # window start
        (20, 3, "seer"),       # repeat — should NOT count as info
        (30, 2, None),
        (40, 3, "seer"),       # another repeat
    ]
    for ts, seat, co in events_seq:
        await store.insert(
            make_npc_generated_event(
                game_id=g.id, phase_id=phase_id, day=1,
                phase=Phase.DAY_DISCUSSION,
                speaker_seat=seat, text=f"seat {seat} ts {ts}",
                co_declaration=co, created_at_ms=ts,
            )
        )
    events = await store.load_phase(g.id, phase_id)
    state = rebuild_public_state_from_events(events)
    assert state is not None
    # 5 events → summary is capped at 6 → all 5 retained.
    # Last 4 entries: (2, False), (3, False), (2, False), (3, False)
    # Pair volley: 2 distinct seats {2,3}, no has_info in window → demote.
    from wolfbot.master.speak_arbiter import _compute_demoted_seats
    assert _compute_demoted_seats(state.recent_speech_summary) == frozenset({2, 3})


async def test_consecutive_cap_demotion_after_3_same_seat() -> None:
    """Same seat speaking 3 in a row → demote that seat. Mostly fires
    when a human keeps re-addressing the same NPC, but defensive against
    any future bug that lets the same seat dispatch repeatedly."""
    from wolfbot.master.speak_arbiter import _compute_demoted_seats

    summary = ((5, False), (5, False), (5, False))
    assert _compute_demoted_seats(summary) == frozenset({5})


async def test_compute_demoted_seats_no_op_for_short_window() -> None:
    """Window shorter than the gate thresholds yields an empty set."""
    from wolfbot.master.speak_arbiter import _compute_demoted_seats

    assert _compute_demoted_seats(()) == frozenset()
    assert _compute_demoted_seats(((1, False), (2, False))) == frozenset()


async def test_try_dispatch_next_diverts_around_pair_volley(
    repo: SqliteRepo,
) -> None:
    """End-to-end: simulate ラキオ (1) ↔ ジョナス (2) ping-pong with no CO,
    then verify the next dispatch targets ジナ (3) — the only third
    online NPC — and the persisted selection_reason is ``low_info_diversion``.
    """
    g = Game(
        id="rv-divert",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=10**12,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    seats = [
        Seat(seat_no=1, display_name="🦋ラキオ", discord_user_id=None,
             is_llm=True, persona_key="raqio"),
        Seat(seat_no=2, display_name="🎩ジョナス", discord_user_id=None,
             is_llm=True, persona_key="jonas"),
        Seat(seat_no=3, display_name="🟣ジナ", discord_user_id=None,
             is_llm=True, persona_key="gina"),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    for sn, role in ((1, Role.WEREWOLF), (2, Role.MADMAN), (3, Role.SEER)):
        await repo.set_player_role(g.id, sn, role)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id, phase_id=phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3], created_at_ms=1,
        )
    )
    from wolfbot.services.discussion_service import make_npc_generated_event
    # First, seed a single seat-3 speech so silent_seats becomes empty.
    # Then 4 alternating no-info speeches between seats 1 and 2 — the
    # last 4 events form the pair-volley window. Without the demotion
    # gate, seat 1 would win as lowest non-last-speaker (LRU). With it,
    # seat 3 is the only non-demoted candidate.
    await store.insert(
        make_npc_generated_event(
            game_id=g.id, phase_id=phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=3, text="ジナの開始発言",
            created_at_ms=5,
        )
    )
    for ts, seat in ((10, 1), (20, 2), (30, 1), (40, 2)):
        await store.insert(
            make_npc_generated_event(
                game_id=g.id, phase_id=phase_id, day=1,
                phase=Phase.DAY_DISCUSSION,
                speaker_seat=seat, text=f"seat {seat}",
                created_at_ms=ts,
            )
        )

    registry = InMemoryNpcRegistry()
    bufs: dict[str, list[str]] = {"raqio": [], "jonas": [], "gina": []}
    for npc_id, persona, seat in (
        ("npc_raqio", "raqio", 1),
        ("npc_jonas", "jonas", 2),
        ("npc_gina", "gina", 3),
    ):
        registry.register(
            npc_id=npc_id, discord_bot_user_id=f"bot_{persona}",
            supported_voices=(), version="1",
            send=_captured_send(bufs[persona]),
            now_ms=1000, persona_key=persona,
        )
        registry.assign(npc_id, seat=seat, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo, registry=registry, discussion=discussion,
        now_ms=lambda: 2000,
    )
    await arb.try_dispatch_next(g.id)

    assert bufs["gina"], "third party (席3 ジナ) must break the volley"
    assert not bufs["raqio"]
    assert not bufs["jonas"]

    seat_repr, reason = await _fetch_selection_reason(repo, g.id)
    assert seat_repr == "3"
    assert reason == "low_info_diversion"


async def test_arbiter_cleanup_game_drops_only_target_game(repo: SqliteRepo) -> None:
    """`cleanup_game` is wired into `_on_reactive_game_end` so a long-lived
    Master process doesn't carry stale `_pending` / `_active_playback` /
    `_playback_deadlines` across games. Two-game scenario: seed pending
    state for both, sweep g1, verify g2 untouched.
    """
    from wolfbot.master.speak_arbiter import _PendingRequest

    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(
        repo=repo,
        registry=InMemoryNpcRegistry(),
        discussion=discussion,
        now_ms=lambda: 1_000,
    )
    # Seed: two pending requests in g1, one in g2 — plus mirroring entries
    # in active_playback / playback_deadlines (the gates the user-facing
    # serial-speech check inspects).
    arb._pending["req-g1-a"] = _PendingRequest(
        request_id="req-g1-a", npc_id="npc1", seat_no=2,
        phase_id="g1::day1::DAY_DISCUSSION::1", game_id="g1",
        expires_at_ms=10_000,
    )
    arb._pending["req-g1-b"] = _PendingRequest(
        request_id="req-g1-b", npc_id="npc2", seat_no=3,
        phase_id="g1::day1::DAY_DISCUSSION::1", game_id="g1",
        expires_at_ms=10_000,
    )
    arb._pending["req-g2-c"] = _PendingRequest(
        request_id="req-g2-c", npc_id="npc3", seat_no=4,
        phase_id="g2::day1::DAY_DISCUSSION::1", game_id="g2",
        expires_at_ms=10_000,
    )
    arb._active_playback.update({"req-g1-a", "req-g2-c"})
    arb._playback_deadlines.update({"req-g1-a": 5_000, "req-g2-c": 5_000})

    swept = arb.cleanup_game("g1")
    assert swept == 2
    # g1 entries gone everywhere.
    assert "req-g1-a" not in arb._pending
    assert "req-g1-b" not in arb._pending
    assert "req-g1-a" not in arb._active_playback
    assert "req-g1-a" not in arb._playback_deadlines
    # g2 entries preserved.
    assert "req-g2-c" in arb._pending
    assert "req-g2-c" in arb._active_playback
    assert arb._playback_deadlines["req-g2-c"] == 5_000


def test_logic_packet_builder_includes_co_claims_in_summary() -> None:
    state = PublicDiscussionState(
        game_id="g",
        phase_id="g::day1::DAY_DISCUSSION::1",
        day=1,
        alive_seat_nos=frozenset({1, 2, 3}),
    )
    # CoClaim is normally derived; build one manually for the unit test.
    from wolfbot.domain.discussion import CoClaim

    state.co_claims = (CoClaim(seat=2, role_claim="seer", declared_at_event_id="e1"),)
    state.silent_seats = frozenset({3})
    packet = build_logic_packet(
        state=state,
        recipient_npc_id="npc_p3",
        expires_at_ms=2000,
        now_ms=1500,
    )
    assert packet.recipient_npc_id == "npc_p3"
    assert packet.expires_at_ms == 2000
    assert any(c.id == "co-2-seer" for c in packet.logic_candidates)
    assert "席2=seer" in packet.public_state_summary
    assert "silent_seats=[3]" in packet.public_state_summary
