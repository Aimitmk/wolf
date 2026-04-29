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
