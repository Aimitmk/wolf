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
        now_ms=1000,
        persona_key="setsu",
    )
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
        now_ms=1000,
        persona_key="setsu",
    )
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
        now_ms=1000,
        persona_key="setsu",
    )
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

    arb = SpeakArbiter(repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 1500)
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
        now_ms=1000,
        persona_key="setsu",
    )
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
        now_ms=1000,
        persona_key="setsu",
    )
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


async def test_role_callout_pool_prioritizes_real_role_and_wolf_side(
    repo: SqliteRepo,
) -> None:
    """When `pending_role_callouts` has 'seer' and no seer has CO'd yet,
    the next dispatch must come from the **callout pool**: real seer +
    every wolf-side seat (人狼/狂人) that hasn't CO'd as any info role.
    Pure villagers must NOT be picked while the pool has eligible
    candidates — this is the user's day-1 priority spec.
    """
    g = Game(
        id="rv-callout-pool",
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
    # Seats: 1=ジョナス WEREWOLF, 2=ジナ VILLAGER, 3=ラキオ MADMAN,
    # 4=コメット VILLAGER, 5=セツ SEER (real), 6=シゲミチ VILLAGER (caller).
    seats = [
        Seat(
            seat_no=1,
            display_name="🎩ジョナス",
            discord_user_id=None,
            is_llm=True,
            persona_key="jonas",
        ),
        Seat(
            seat_no=2, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
        Seat(
            seat_no=3,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
        Seat(
            seat_no=4,
            display_name="☄️コメット",
            discord_user_id=None,
            is_llm=True,
            persona_key="comet",
        ),
        Seat(
            seat_no=5, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(
            seat_no=6,
            display_name="👽シゲミチ",
            discord_user_id=None,
            is_llm=True,
            persona_key="shigemichi",
        ),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    for sn, role in (
        (1, Role.WEREWOLF),
        (2, Role.VILLAGER),
        (3, Role.MADMAN),
        (4, Role.VILLAGER),
        (5, Role.SEER),
        (6, Role.VILLAGER),
    ):
        await repo.set_player_role(g.id, sn, role)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3, 4, 5, 6],
            created_at_ms=1,
        )
    )
    # シゲミチ asks "誰か占い師?" — the analyzer would tag this as
    # role_callout="seer". Insert a synthetic NPC speech event with the
    # callout to simulate that.
    from wolfbot.services.discussion_service import make_npc_generated_event

    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=6,
            text="誰か占い師、名乗ってくれ！",
            role_callout="seer",
            created_at_ms=10,
        )
    )

    registry = InMemoryNpcRegistry()
    bufs: dict[int, list[str]] = {n: [] for n in (1, 2, 3, 4, 5, 6)}
    persona_by_seat = {
        1: "jonas",
        2: "gina",
        3: "raqio",
        4: "comet",
        5: "setsu",
        6: "shigemichi",
    }
    for seat_no, persona in persona_by_seat.items():
        registry.register(
            npc_id=f"npc_{persona}",
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[seat_no]),
            now_ms=2000,
            persona_key=persona,
        )
        registry.assign(f"npc_{persona}", seat=seat_no, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 3000,
    )

    # Dispatch — must go to one of the pool members:
    # real seer (5) + wolves+madman (1, 3). Pure villagers (2, 4, 6) must
    # NOT be picked.
    await arb.try_dispatch_next(g.id)
    pool_seats = {1, 3, 5}
    non_pool_seats = {2, 4, 6}
    picked = next((s for s, b in bufs.items() if any('"speak_request"' in m for m in b)), None)
    assert picked is not None, "must dispatch someone"
    assert picked in pool_seats, (
        f"picked seat {picked} must be in callout pool {pool_seats} "
        f"(real seer + wolves + madman, all uncpd). "
        f"villagers {non_pool_seats} should NOT be picked while pool is non-empty."
    )

    _, reason = await _fetch_selection_reason(repo, g.id)
    assert reason == "role_callout_pool"


async def test_first_seer_co_fires_counter_co_pool(
    repo: SqliteRepo,
) -> None:
    """When the real seer is the first to CO with no prior callout,
    the next dispatch must still come from the pool — pool = uncpd
    wolf-side (the real seer is excluded because they just CO'd).

    This is the user's 2026-05-01 spec: a single-CO situation should
    open a guaranteed counter-CO opportunity window so a fake CO from
    a wolf has a chance to surface, instead of the village-side
    accidentally treating an unchallenged CO as gospel.
    """
    g = Game(
        id="rv-first-co",
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
        Seat(
            seat_no=1,
            display_name="🎩ジョナス",
            discord_user_id=None,
            is_llm=True,
            persona_key="jonas",
        ),
        Seat(
            seat_no=2, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
        Seat(
            seat_no=3,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
        Seat(
            seat_no=4,
            display_name="☄️コメット",
            discord_user_id=None,
            is_llm=True,
            persona_key="comet",
        ),
        Seat(
            seat_no=5, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(
            seat_no=6,
            display_name="👽シゲミチ",
            discord_user_id=None,
            is_llm=True,
            persona_key="shigemichi",
        ),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    for sn, role in (
        (1, Role.WEREWOLF),
        (2, Role.VILLAGER),
        (3, Role.MADMAN),
        (4, Role.VILLAGER),
        (5, Role.SEER),
        (6, Role.VILLAGER),
    ):
        await repo.set_player_role(g.id, sn, role)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3, 4, 5, 6],
            created_at_ms=1,
        )
    )
    # Real seer (seat 5) declares first. No prior callout — the
    # window is opened purely by the new ``pending_co_response``
    # mechanism.
    from wolfbot.services.discussion_service import make_npc_generated_event

    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=5,
            text="実は私、占い師なのです。昨夜、ジョナスを占いました。",
            co_declaration="seer",
            created_at_ms=10,
        )
    )

    registry = InMemoryNpcRegistry()
    bufs: dict[int, list[str]] = {n: [] for n in (1, 2, 3, 4, 5, 6)}
    persona_by_seat = {
        1: "jonas",
        2: "gina",
        3: "raqio",
        4: "comet",
        5: "setsu",
        6: "shigemichi",
    }
    for seat_no, persona in persona_by_seat.items():
        registry.register(
            npc_id=f"npc_{persona}",
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[seat_no]),
            now_ms=2000,
            persona_key=persona,
        )
        registry.assign(
            f"npc_{persona}",
            seat=seat_no,
            game_id=g.id,
            phase_id=phase_id,
        )

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 3000,
    )

    await arb.try_dispatch_next(g.id)

    # Pool: real seer (5) is now CO'd → excluded. Uncpd wolf-side =
    # ジョナス (1) + ラキオ (3). Villagers 2/4/6 must stay out.
    pool_seats = {1, 3}
    non_pool_seats = {2, 4, 5, 6}
    picked = next(
        (s for s, b in bufs.items() if any('"speak_request"' in m for m in b)),
        None,
    )
    assert picked is not None, "must dispatch someone"
    assert picked in pool_seats, (
        f"picked seat {picked} must be in counter-CO pool {pool_seats} "
        f"(uncpd wolf-side after the real seer's first CO). "
        f"Non-pool seats {non_pool_seats} (villagers + the CO'er) "
        f"should NOT be picked."
    )
    _, reason = await _fetch_selection_reason(repo, g.id)
    assert reason == "role_callout_pool"


async def test_first_co_pool_skips_text_mismatch_co(
    repo: SqliteRepo,
) -> None:
    """A SpeechEvent with structured ``co_declaration='seer'`` whose
    text is a counter-CO request rather than a self-declaration must
    NOT open the counter-CO pool — the text-vs-structured guard drops
    the leaked structured flag, so ``pending_co_response`` stays
    empty and dispatch falls through to normal priority.

    Reproduces game ``98e5a083b5ff`` day 1 ラキオの誤検知.
    """
    g = Game(
        id="rv-first-co-mismatch",
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
        Seat(
            seat_no=1,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
        Seat(
            seat_no=2, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.MADMAN)
    await repo.set_player_role(g.id, 2, Role.SEER)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2],
            created_at_ms=1,
        )
    )
    # Leaked structured flag: text is a counter-CO request, not a
    # declaration. The guard must drop the flag → no CO recorded →
    # pool stays inactive.
    from wolfbot.services.discussion_service import make_npc_generated_event

    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=1,
            text="ステラ、対抗占い師は出ないのか？早く名乗りなさい。",
            co_declaration="seer",
            created_at_ms=10,
        )
    )

    # Rebuild via the canonical fold path (= what restart recovery /
    # arbiter prompt build use). ``pending_co_response`` should be
    # empty, signalling the guard worked.
    from wolfbot.services.discussion_service import (
        rebuild_public_state_from_events,
    )

    events = await store.load_phase(g.id, phase_id)
    state = rebuild_public_state_from_events(events)
    assert state is not None
    assert state.pending_co_response == frozenset()
    assert state.co_claims == ()


async def test_info_request_callout_expands_pool_to_all_info_roles(
    repo: SqliteRepo,
) -> None:
    """A generic info-seeking speech (「誰か怪しい人?」「みんな意見聞かせて」)
    is tagged role_callout='info_request' by the analyzer. The arbiter
    must treat this like a callout for ALL info roles simultaneously:
    pool = real seer + real medium + real knight + every uncpd wolf-side.
    Pure villagers stay out of the pool."""
    g = Game(
        id="rv-info-request",
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
        Seat(
            seat_no=1,
            display_name="🎩ジョナス",
            discord_user_id=None,
            is_llm=True,
            persona_key="jonas",
        ),  # WEREWOLF
        Seat(
            seat_no=2, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),  # MEDIUM
        Seat(
            seat_no=3,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),  # VILLAGER (caller)
        Seat(
            seat_no=4,
            display_name="☄️コメット",
            discord_user_id=None,
            is_llm=True,
            persona_key="comet",
        ),  # VILLAGER
        Seat(
            seat_no=5, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),  # MADMAN
        Seat(
            seat_no=6,
            display_name="👽シゲミチ",
            discord_user_id=None,
            is_llm=True,
            persona_key="shigemichi",
        ),  # SEER
        Seat(
            seat_no=7, display_name="🍎SQ", discord_user_id=None, is_llm=True, persona_key="sq"
        ),  # KNIGHT
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    for sn, role in (
        (1, Role.WEREWOLF),
        (2, Role.MEDIUM),
        (3, Role.VILLAGER),
        (4, Role.VILLAGER),
        (5, Role.MADMAN),
        (6, Role.SEER),
        (7, Role.KNIGHT),
    ):
        await repo.set_player_role(g.id, sn, role)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3, 4, 5, 6, 7],
            created_at_ms=1,
        )
    )
    # Caller (ラキオ, villager): 「誰か怪しい人挙げて」 → role_callout='info_request'
    from wolfbot.services.discussion_service import make_npc_generated_event

    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=3,
            text="誰か怪しい人挙げてくれ！",
            role_callout="info_request",
            created_at_ms=10,
        )
    )

    registry = InMemoryNpcRegistry()
    bufs: dict[int, list[str]] = {n: [] for n in range(1, 8)}
    persona_by_seat = {
        1: "jonas",
        2: "gina",
        3: "raqio",
        4: "comet",
        5: "setsu",
        6: "shigemichi",
        7: "sq",
    }
    for seat_no, persona in persona_by_seat.items():
        registry.register(
            npc_id=f"npc_{persona}",
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[seat_no]),
            now_ms=2000,
            persona_key=persona,
        )
        registry.assign(f"npc_{persona}", seat=seat_no, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 3000,
    )
    await arb.try_dispatch_next(g.id)

    # Pool: seats 1 (wolf), 2 (medium), 5 (madman), 6 (seer), 7 (knight).
    # Pure villagers 3, 4 must NOT be picked.
    pool_seats = {1, 2, 5, 6, 7}
    villager_seats = {3, 4}
    picked = next(
        (s for s, b in bufs.items() if any('"speak_request"' in m for m in b)),
        None,
    )
    assert picked is not None, "must dispatch from the info_request pool"
    assert picked in pool_seats, (
        f"info_request pool must include real info roles + wolf-side. "
        f"picked={picked} pool_seats={pool_seats}, villagers {villager_seats} "
        f"should never be picked while pool is non-empty."
    )
    _, reason = await _fetch_selection_reason(repo, g.id)
    assert reason == "role_callout_pool"


async def test_info_request_consumed_when_any_info_role_cod(
    repo: SqliteRepo,
) -> None:
    """info_request is a generic callout; once anyone CO's any info role
    (seer/medium/knight), the priority pool steps down and normal
    rotation resumes (villagers can be picked again)."""
    g = Game(
        id="rv-info-request-consume",
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
        Seat(
            seat_no=1,
            display_name="🎩ジョナス",
            discord_user_id=None,
            is_llm=True,
            persona_key="jonas",
        ),  # SEER (will CO)
        Seat(
            seat_no=2, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),  # VILLAGER
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.SEER)
    await repo.set_player_role(g.id, 2, Role.VILLAGER)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2],
            created_at_ms=1,
        )
    )
    from wolfbot.services.discussion_service import make_npc_generated_event

    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=2,
            text="誰か怪しい人挙げて",
            role_callout="info_request",
            created_at_ms=10,
        )
    )
    # Real seer answers — co_declaration='seer'.
    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=1,
            text="私が占い師だ",
            co_declaration="seer",
            created_at_ms=20,
        )
    )

    # Now rebuild state — info_request must have been consumed by the
    # seer CO. pending_role_callouts should NOT contain "info_request".
    from wolfbot.services.discussion_service import (
        rebuild_public_state_from_events,
    )

    events = await store.load_phase(g.id, phase_id)
    state = rebuild_public_state_from_events(events)
    assert state is not None
    assert "info_request" not in state.pending_role_callouts, (
        "any info-role CO must consume the info_request callout"
    )


async def test_role_callout_pool_excludes_seats_that_already_cod(
    repo: SqliteRepo,
) -> None:
    """A wolf who already fake-CO'd as seer must be excluded from the pool
    (no double-counting). The pool only contains uncpd info-role candidates.
    """
    g = Game(
        id="rv-callout-pool-exclude",
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
        Seat(
            seat_no=1,
            display_name="🎩ジョナス",
            discord_user_id=None,
            is_llm=True,
            persona_key="jonas",
        ),  # WEREWOLF — fake-CO'd already
        Seat(
            seat_no=2, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),  # MADMAN — still in pool
        Seat(
            seat_no=3, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),  # SEER (real) — still in pool
        Seat(
            seat_no=4,
            display_name="☄️コメット",
            discord_user_id=None,
            is_llm=True,
            persona_key="comet",
        ),  # VILLAGER — never in pool
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    for sn, role in (
        (1, Role.WEREWOLF),
        (2, Role.MADMAN),
        (3, Role.SEER),
        (4, Role.VILLAGER),
    ):
        await repo.set_player_role(g.id, sn, role)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3, 4],
            created_at_ms=1,
        )
    )
    from wolfbot.services.discussion_service import make_npc_generated_event

    # Wolf already fake-CO'd as seer (consumes the original callout).
    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=1,
            text="私が占い師だ",
            co_declaration="seer",
            created_at_ms=10,
        )
    )
    # Then someone else asks "他に占い師は?" — re-fires the seer callout.
    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=4,
            text="他に占い師の方は？",
            role_callout="seer",
            created_at_ms=20,
        )
    )

    registry = InMemoryNpcRegistry()
    bufs: dict[int, list[str]] = {n: [] for n in (1, 2, 3, 4)}
    persona_by_seat = {1: "jonas", 2: "gina", 3: "setsu", 4: "comet"}
    for seat_no, persona in persona_by_seat.items():
        registry.register(
            npc_id=f"npc_{persona}",
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[seat_no]),
            now_ms=2000,
            persona_key=persona,
        )
        registry.assign(f"npc_{persona}", seat=seat_no, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 3000,
    )

    await arb.try_dispatch_next(g.id)

    # Pool should contain seat 2 (madman, no CO) and seat 3 (real seer).
    # Seat 1 already CO'd → excluded. Seat 4 villager → never in pool.
    picked = next((s for s, b in bufs.items() if any('"speak_request"' in m for m in b)), None)
    assert picked is not None
    assert picked in {2, 3}, (
        f"picked seat {picked} must be madman (2) or real seer (3); "
        f"wolf (1) is already CO'd, villager (4) never in pool."
    )


async def test_role_callout_pool_asked_tracker_avoids_repick(
    repo: SqliteRepo,
) -> None:
    """If a pool member declines, the asked-tracker must prevent the picker
    from looping on the same seat. Subsequent dispatches go to other pool
    members until everyone has had one chance."""
    g = Game(
        id="rv-callout-asked",
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
        Seat(
            seat_no=1,
            display_name="🎩ジョナス",
            discord_user_id=None,
            is_llm=True,
            persona_key="jonas",
        ),
        Seat(
            seat_no=2, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 2, Role.SEER)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2],
            created_at_ms=1,
        )
    )
    from wolfbot.services.discussion_service import make_npc_generated_event

    # External callout — fired by no-one in the test (we add a fake event
    # with a non-pool speaker_seat, but to avoid violating the alive-set
    # we attribute it to seat 1 with role_callout but no co_declaration).
    # Actually simpler: use seat 2 itself but set co_declaration=None;
    # then the callout fires without consuming itself.
    # We need a third seat to act as the speaker — let's add one as
    # already-dead and not part of the pool, but the model requires
    # alive=True for speakers to be valid... use a knight-like spectator.
    # Workaround: use seat 1 (wolf) as the question asker. Their event
    # only flags role_callout=seer; the wolf itself stays in pool because
    # they didn't co_declare.
    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=1,
            text="占い師は誰だ？",
            role_callout="seer",
            created_at_ms=10,
        )
    )

    registry = InMemoryNpcRegistry()
    bufs: dict[int, list[str]] = {1: [], 2: []}
    persona_by_seat = {1: "jonas", 2: "setsu"}
    for seat_no, persona in persona_by_seat.items():
        registry.register(
            npc_id=f"npc_{persona}",
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[seat_no]),
            now_ms=2000,
            persona_key=persona,
        )
        registry.assign(f"npc_{persona}", seat=seat_no, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 3000,
    )

    # First dispatch from pool — pick is random between 1 and 2.
    await arb.try_dispatch_next(g.id)
    first_picked = next(
        (s for s, b in bufs.items() if any('"speak_request"' in m for m in b)), None
    )
    assert first_picked in {1, 2}

    # The picked seat is now in `_callout_pool_asked`. Second dispatch
    # MUST go to the other pool member (asked tracker excludes the first).
    bufs[1].clear()
    bufs[2].clear()
    # Need to clear _pending so dispatch can proceed (no playback yet).
    arb._pending.clear()
    await arb.try_dispatch_next(g.id)
    second_picked = next(
        (s for s, b in bufs.items() if any('"speak_request"' in m for m in b)), None
    )
    assert second_picked is not None
    assert second_picked != first_picked, (
        f"asked tracker must steer to a different pool member: "
        f"first={first_picked}, second={second_picked}"
    )


async def test_speak_result_rejection_clears_pending_and_redispatches(
    repo: SqliteRepo,
) -> None:
    """Regression for game eab1f9514a10 day 4: SQ finished, ユリコ was
    dispatched but took 10s to respond (TTL=8s) → `expired_request`
    rejection. The old rejection path neither popped `_pending` nor called
    `try_dispatch_next`, so the day stalled silently for 2.5 minutes.

    After the fix, every valid-pending rejection (expired/stale/declined/
    too-long) must:
      1. Pop _pending so the slot is freed.
      2. Call try_dispatch_next so another candidate gets a chance.
    """
    g = Game(
        id="rv-reject-redispatch",
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
        Seat(
            seat_no=1,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
        Seat(
            seat_no=2, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 2, Role.SEER)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2],
            created_at_ms=1,
        )
    )

    registry = InMemoryNpcRegistry()
    bufs: dict[str, list[str]] = {"raqio": [], "setsu": []}
    for npc_id, persona, seat_no in (
        ("npc_raqio", "raqio", 1),
        ("npc_setsu", "setsu", 2),
    ):
        registry.register(
            npc_id=npc_id,
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[persona]),
            now_ms=1000,
            persona_key=persona,
        )
        registry.assign(npc_id, seat=seat_no, game_id=g.id, phase_id=phase_id)

    # Step 1: dispatch seat 1 (ラキオ). Use a permissive heartbeat_timeout
    # since the test artificially advances `now` past the request TTL — in
    # production the NPC sends heartbeats continuously so stale clock vs
    # heartbeat doesn't happen.
    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 2000,
        config=SpeakArbiterConfig(heartbeat_timeout_ms=60_000),
    )
    state = await arb.rebuild_public_state(
        game_id=g.id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
    )
    assert state is not None
    req, _ = await arb.dispatch_request(
        state=state, candidate_npc_id="npc_raqio", seat_no=1, game_id=g.id
    )
    assert req is not None
    assert req.request_id in arb._pending

    # Step 2: simulate now > expires_at_ms by advancing the arbiter's clock
    # past the request's TTL.
    arb._now_ms = lambda: req.expires_at_ms + 100  # type: ignore[method-assign]

    bufs["raqio"].clear()
    bufs["setsu"].clear()

    # Step 3: SpeakResult arrives late → expired_request rejection.
    result = SpeakResult(
        ts=req.expires_at_ms + 100,
        trace_id="t",
        request_id=req.request_id,
        npc_id="npc_raqio",
        phase_id=phase_id,
        status="accepted",
        text="late response",
    )
    ok, reason = await arb.handle_speak_result(
        result,
        current_phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
    )
    assert not ok and reason == "expired_request"

    # Verify _pending was popped (slot freed).
    assert req.request_id not in arb._pending, (
        "_pending must be popped on rejection so the slot is freed"
    )

    # Verify try_dispatch_next was called → another seat got a SpeakRequest.
    # Without the fix, both bufs would be empty and the day would stall.
    # (The picker may choose either seat 1 or seat 2 depending on RNG —
    # both have count=0 and no addressed bias.)
    new_request_msgs = [m for m in (*bufs["raqio"], *bufs["setsu"]) if '"speak_request"' in m]
    assert new_request_msgs, (
        "rejection must trigger try_dispatch_next; another candidate must be picked. "
        f"raqio buf: {bufs['raqio']!r}, setsu buf: {bufs['setsu']!r}"
    )


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
        now_ms=1000,
        persona_key="setsu",
    )
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
        now_ms=1000,
        persona_key="setsu",
    )
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
        Seat(
            seat_no=2, display_name="セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(
            seat_no=3, display_name="ジーナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
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


async def _fetch_selection_reason(repo: SqliteRepo, game_id: str) -> tuple[str | None, str | None]:
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
        Seat(
            seat_no=2, display_name="セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(
            seat_no=3, display_name="ジーナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
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
        Seat(
            seat_no=2, display_name="セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(
            seat_no=3, display_name="ジーナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
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
        Seat(
            seat_no=1,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
        Seat(
            seat_no=2, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(
            seat_no=3, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
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
                game_id=g.id,
                phase_id=phase_id,
                day=1,
                phase=Phase.DAY_DISCUSSION,
                speaker_seat=seat_no,
                text=text,
                created_at_ms=ts,
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
            npc_id=npc_id,
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[persona]),
            now_ms=1000,
            persona_key=persona,
        )
        registry.assign(npc_id, seat=seat_no, game_id=g.id, phase_id=phase_id)

    import random as _random

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 2000,
        rng=_random.Random(0),
    )
    await arb.try_dispatch_next(g.id)

    assert not bufs["raqio"], "ラキオ was the immediate previous speaker — must NOT be re-picked"
    # Equal-priority tiebreak is randomised — seat 2 OR seat 3 may win,
    # but never seat 1 (the just-spoken seat). Seeded RNG keeps the
    # specific winner deterministic for the test (currently seat 2).
    assert bufs["setsu"] or bufs["gina"]
    if bufs["setsu"]:
        assert not bufs["gina"]
    else:
        assert not bufs["setsu"]


async def test_try_dispatch_next_prefers_lower_speech_count(
    repo: SqliteRepo,
) -> None:
    """The seat with the lowest phase-wide speech_count wins, even when
    every alive NPC has spoken at least once (so silent_seats is empty).

    Reproduces the in-the-wild imbalance where the lowest-seat-number NPC
    monopolised once the silent rotation completed: with the binary
    silent_seats only, seat 1 with count=4 still tied with seat 3 at
    count=1 on the silent axis and won by seat tiebreak. The new sort
    treats count as a continuous signal so seat 3 is preferred.
    """
    g = Game(
        id="rv-count",
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
        Seat(
            seat_no=1,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
        Seat(
            seat_no=2, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(
            seat_no=3, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 2, Role.MADMAN)
    await repo.set_player_role(g.id, 3, Role.SEER)

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
    from wolfbot.services.discussion_service import make_npc_generated_event

    # Counts after seeding: seat1=4, seat2=4, seat3=1. Seat 3 also speaks
    # in the recent window (event ts=60) so the pair-volley demotion
    # gate stays quiet — the only differentiator left is the per-seat
    # speech_count. Without that axis seat 1 wins by lowest seat tie-
    # break; with it seat 3 beats both 4-counts.
    #
    # No CO is emitted in this fixture because a first-CO would fire
    # the counter-CO opportunity pool and override low-count rotation
    # — that pool path is exercised separately. Here we want to pin the
    # bare ``low_count_rotation`` reason without crossing pool lines.
    payload = [
        (10, 1, "ラキオ1巡目"),  # 1:1
        (20, 2, "セツの所感"),  # 2:1
        (30, 1, "ラキオ反論1"),  # 1:2
        (40, 2, "セツ反応1"),  # 2:2
        (50, 1, "ラキオ反論2"),  # 1:3
        (55, 2, "セツ反応2"),  # 2:3
        (60, 3, "ジナの差し込み"),  # 3:1 (breaks pair window)
        (70, 1, "ラキオ反論3"),  # 1:4
        (80, 2, "セツ反応3"),  # 2:4
    ]
    for ts, seat, text in payload:
        kwargs: dict[str, object] = dict(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=seat,
            text=text,
            created_at_ms=ts,
        )
        await store.insert(make_npc_generated_event(**kwargs))  # type: ignore[arg-type]

    registry = InMemoryNpcRegistry()
    bufs: dict[str, list[str]] = {"raqio": [], "setsu": [], "gina": []}
    for npc_id, persona, seat_no in (
        ("npc_raqio", "raqio", 1),
        ("npc_setsu", "setsu", 2),
        ("npc_gina", "gina", 3),
    ):
        registry.register(
            npc_id=npc_id,
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[persona]),
            now_ms=1000,
            persona_key=persona,
        )
        registry.assign(npc_id, seat=seat_no, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 2000,
    )
    await arb.try_dispatch_next(g.id)

    assert bufs["gina"], "ジナ (count=1) must outrank counts of 4"
    assert not bufs["raqio"]
    assert not bufs["setsu"]

    seat_repr, reason = await _fetch_selection_reason(repo, g.id)
    assert seat_repr == "3"
    assert reason == "low_count_rotation"


async def test_try_dispatch_next_lru_when_speech_counts_tied(
    repo: SqliteRepo,
) -> None:
    """Two NPCs at equal speech_count — the just-spoke seat is demoted
    (LRU), and the remaining seat wins. The reason is ``lru_rotation``,
    not ``low_count_rotation`` (no count differential to leverage).
    """
    g = Game(
        id="rv-lru-tied",
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
        Seat(
            seat_no=1,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
        Seat(
            seat_no=2, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 2, Role.SEER)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2],
            created_at_ms=1,
        )
    )
    from wolfbot.services.discussion_service import make_npc_generated_event

    # seat 2 occupies the just-spoke slot at the end so LRU pushes us
    # back to seat 1. Both seats have count=2 so the count axis is
    # neutral.
    #
    # Seat 1's first event carries a knight CO with valid self-decl
    # text — chosen because (a) the post-2026-05-01 ``co_declaration``
    # consistency guard accepts it (text + structured field agree),
    # (b) the game has no real knight role, so the resulting first-CO
    # counter-CO pool is empty (no real role-holder, no other
    # uncommitted wolf-side seat), keeping LRU as the operative axis.
    # This pads the recent-speech-summary with one ``has_info=True``
    # entry, which silences the pair-volley demotion gate that would
    # otherwise demote both seats and reroute through the
    # ``all_demoted_fallback`` path.
    co_setup = {(10, 1): ("実は私、騎士なんだ。", "knight")}
    for ts, seat in ((10, 1), (20, 2), (30, 1), (40, 2)):
        text, co = co_setup.get((ts, seat), (f"seat {seat} ts={ts}", None))
        await store.insert(
            make_npc_generated_event(
                game_id=g.id,
                phase_id=phase_id,
                day=1,
                phase=Phase.DAY_DISCUSSION,
                speaker_seat=seat,
                text=text,
                co_declaration=co,
                created_at_ms=ts,
            )
        )

    registry = InMemoryNpcRegistry()
    bufs: dict[str, list[str]] = {"raqio": [], "setsu": []}
    for npc_id, persona, seat_no in (
        ("npc_raqio", "raqio", 1),
        ("npc_setsu", "setsu", 2),
    ):
        registry.register(
            npc_id=npc_id,
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[persona]),
            now_ms=1000,
            persona_key=persona,
        )
        registry.assign(npc_id, seat=seat_no, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 2000,
    )
    await arb.try_dispatch_next(g.id)

    assert bufs["raqio"]
    assert not bufs["setsu"]
    seat_repr, reason = await _fetch_selection_reason(repo, g.id)
    assert seat_repr == "1"
    assert reason == "lru_rotation"


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
        Seat(seat_no=1, display_name="Alice", discord_user_id="u1", is_llm=False, persona_key=None),
        Seat(
            seat_no=2,
            display_name="🎩ジョナス",
            discord_user_id=None,
            is_llm=True,
            persona_key="jonas",
        ),
        Seat(
            seat_no=3,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
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
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3],
            created_at_ms=1,
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
        (5, 3, "seer"),  # first CO (outside the last-4 window)
        (10, 2, None),  # window start
        (20, 3, "seer"),  # repeat — should NOT count as info
        (30, 2, None),
        (40, 3, "seer"),  # another repeat
    ]
    for ts, seat, co in events_seq:
        await store.insert(
            make_npc_generated_event(
                game_id=g.id,
                phase_id=phase_id,
                day=1,
                phase=Phase.DAY_DISCUSSION,
                speaker_seat=seat,
                text=f"seat {seat} ts {ts}",
                co_declaration=co,
                created_at_ms=ts,
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
        Seat(
            seat_no=1,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
        Seat(
            seat_no=2,
            display_name="🎩ジョナス",
            discord_user_id=None,
            is_llm=True,
            persona_key="jonas",
        ),
        Seat(
            seat_no=3, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
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
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3],
            created_at_ms=1,
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
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=3,
            text="ジナの開始発言",
            created_at_ms=5,
        )
    )
    for ts, seat in ((10, 1), (20, 2), (30, 1), (40, 2)):
        await store.insert(
            make_npc_generated_event(
                game_id=g.id,
                phase_id=phase_id,
                day=1,
                phase=Phase.DAY_DISCUSSION,
                speaker_seat=seat,
                text=f"seat {seat}",
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
            npc_id=npc_id,
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[persona]),
            now_ms=1000,
            persona_key=persona,
        )
        registry.assign(npc_id, seat=seat, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 2000,
    )
    await arb.try_dispatch_next(g.id)

    assert bufs["gina"], "third party (席3 ジナ) must break the volley"
    assert not bufs["raqio"]
    assert not bufs["jonas"]

    seat_repr, reason = await _fetch_selection_reason(repo, g.id)
    assert seat_repr == "3"
    assert reason == "low_info_diversion"


async def test_repeated_co_across_days_does_not_block_volley_demotion(
    repo: SqliteRepo,
) -> None:
    """Game a701a7531dca day 2 escape hatch: a seat that CO'd on day 1
    re-asserts the same CO on day 2; the per-phase rebuild must NOT treat
    the day-2 re-assertion as fresh info, otherwise the ジョナス↔ユリコ
    ping-pong escapes the `_PAIR_VOLLEY_WINDOW` demotion gate.

    Setup:
      day 1 phase: seat 1 CO seer (recorded in game-wide history).
      day 2 phase: seat 1 re-asserts CO seer, then 1↔9 alternate 4 times.
    Expected: third party (seat 3) wins the next dispatch — `is_new_co`
    must be False for the day-2 re-CO so all 4 window entries are low-info.
    """
    g = Game(
        id="rv-cross-day-co",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=2,
        deadline_epoch=10**12,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    seats = [
        Seat(
            seat_no=1,
            display_name="🎩ジョナス",
            discord_user_id=None,
            is_llm=True,
            persona_key="jonas",
        ),
        Seat(
            seat_no=3, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
        Seat(
            seat_no=9,
            display_name="👑ユリコ",
            discord_user_id=None,
            is_llm=True,
            persona_key="yuriko",
        ),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    for sn, role in ((1, Role.SEER), (3, Role.VILLAGER), (9, Role.VILLAGER)):
        await repo.set_player_role(g.id, sn, role)

    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    from wolfbot.services.discussion_service import make_npc_generated_event

    # Day 1 phase: seat 1 declares seer once.
    day1_phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=day1_phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 3, 9],
            created_at_ms=1,
        )
    )
    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=day1_phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=1,
            text="私が占い師だ",
            co_declaration="seer",
            created_at_ms=10,
        )
    )

    # Day 2 phase: seat 3 opens (so silent_seats is empty), then 1<->9
    # alternate. Seat 1's day-2 utterance carries co_declaration="seer"
    # again — must be treated as a re-assertion (no fresh info).
    day2_phase_id = make_phase_id(g.id, 2, Phase.DAY_DISCUSSION)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=day2_phase_id,
            day=2,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 3, 9],
            created_at_ms=1000,
        )
    )
    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=day2_phase_id,
            day=2,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=3,
            text="day2 開始発言",
            created_at_ms=1005,
        )
    )
    for ts, seat in ((1010, 9), (1020, 1), (1030, 9), (1040, 1)):
        await store.insert(
            make_npc_generated_event(
                game_id=g.id,
                phase_id=day2_phase_id,
                day=2,
                phase=Phase.DAY_DISCUSSION,
                speaker_seat=seat,
                text=f"seat {seat} day2",
                co_declaration="seer" if seat == 1 else None,
                created_at_ms=ts,
            )
        )

    registry = InMemoryNpcRegistry()
    bufs: dict[str, list[str]] = {"jonas": [], "gina": [], "yuriko": []}
    for npc_id, persona, seat_no in (
        ("npc_jonas", "jonas", 1),
        ("npc_gina", "gina", 3),
        ("npc_yuriko", "yuriko", 9),
    ):
        registry.register(
            npc_id=npc_id,
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[persona]),
            now_ms=2000,
            persona_key=persona,
        )
        registry.assign(npc_id, seat=seat_no, game_id=g.id, phase_id=day2_phase_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 3000,
    )
    await arb.try_dispatch_next(g.id)

    assert bufs["gina"], "third party seat 3 must break the volley"
    assert not bufs["jonas"], "seat 1 must be demoted (cross-phase re-CO is not info)"
    assert not bufs["yuriko"], "seat 9 must be demoted"

    seat_repr, reason = await _fetch_selection_reason(repo, g.id)
    assert seat_repr == "3"
    assert reason == "low_info_diversion"


async def test_handle_tts_failed_pops_pending_before_returning(
    repo: SqliteRepo,
) -> None:
    """`handle_tts_failed` releases the playback gate AND removes the
    `_pending` row in the same call. The wiring in `main._on_tts_failed`
    therefore has to read game_id BEFORE invoking the handler — production
    bug had the lookup *after* the call, returning None, and the next
    NPC was never dispatched (silent stall after a VOICEVOX timeout).

    This test documents the contract so future refactors keep the
    "capture game_id first" invariant.
    """
    from wolfbot.domain.ws_messages import TtsFailed
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
    g = Game(
        id="rv-tts-fail",
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
    arb._pending["sr-x"] = _PendingRequest(
        request_id="sr-x",
        npc_id="npc1",
        seat_no=2,
        phase_id="rv-tts-fail::day1::DAY_DISCUSSION::1",
        game_id=g.id,
        expires_at_ms=10_000,
    )
    arb._active_playback.add("sr-x")
    arb._playback_deadlines["sr-x"] = 5_000
    # Open the playback row so handle_tts_failed has something to close.
    await repo.open_npc_playback(
        request_id="sr-x",
        game_id=g.id,
        phase_id="rv-tts-fail::day1::DAY_DISCUSSION::1",
        npc_id="npc1",
        speech_event_id="se-x",
        authorized_at_ms=1_000,
        playback_deadline_ms=5_000,
    )

    captured_game_id = arb._pending["sr-x"].game_id

    msg = TtsFailed(
        ts=2_000,
        trace_id="t-x",
        request_id="sr-x",
        npc_id="npc1",
        failure_reason="voicevox_timeout",
    )
    await arb.handle_tts_failed(msg)

    # Contract: post-call, _pending no longer has the entry. If the
    # main.py wiring reads game_id AFTER calling handle_tts_failed, it
    # gets None and the dispatch chain stalls.
    assert "sr-x" not in arb._pending
    assert "sr-x" not in arb._active_playback
    assert captured_game_id == g.id  # what the wiring should have captured pre-call


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
        request_id="req-g1-a",
        npc_id="npc1",
        seat_no=2,
        phase_id="g1::day1::DAY_DISCUSSION::1",
        game_id="g1",
        expires_at_ms=10_000,
    )
    arb._pending["req-g1-b"] = _PendingRequest(
        request_id="req-g1-b",
        npc_id="npc2",
        seat_no=3,
        phase_id="g1::day1::DAY_DISCUSSION::1",
        game_id="g1",
        expires_at_ms=10_000,
    )
    arb._pending["req-g2-c"] = _PendingRequest(
        request_id="req-g2-c",
        npc_id="npc3",
        seat_no=4,
        phase_id="g2::day1::DAY_DISCUSSION::1",
        game_id="g2",
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


async def test_multi_addressed_seats_both_get_priority(
    repo: SqliteRepo,
) -> None:
    """When an utterance addresses multiple seats (e.g.
    ``addressed_seat_nos=[2, 3]`` for 「セツとジナはどう?」), both seats
    win the addressed-priority axis on the next dispatch. Picking 2
    consumes only its slot, leaving 3 still in the addressed set so the
    follow-up dispatch picks 3 next over a non-addressed candidate.
    """
    import random as _random

    g = Game(
        id="rv-multi-addr",
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
        Seat(
            seat_no=1,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
        Seat(
            seat_no=2, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(
            seat_no=3, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
        Seat(
            seat_no=4,
            display_name="🎩ジョナス",
            discord_user_id=None,
            is_llm=True,
            persona_key="jonas",
        ),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    for sn, role in (
        (1, Role.WEREWOLF),
        (2, Role.SEER),
        (3, Role.MEDIUM),
        (4, Role.VILLAGER),
    ):
        await repo.set_player_role(g.id, sn, role)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 3, 4],
            created_at_ms=1,
        )
    )
    # Raqio (seat 1) addresses BOTH Setsu (2) and Gina (3) in one breath.
    from wolfbot.services.discussion_service import make_npc_generated_event

    await store.insert(
        make_npc_generated_event(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=1,
            text="セツとジナはどう思う?",
            addressed_seat_nos=(2, 3),
            created_at_ms=10,
        )
    )

    registry = InMemoryNpcRegistry()
    bufs: dict[str, list[str]] = {
        "raqio": [],
        "setsu": [],
        "gina": [],
        "jonas": [],
    }
    for npc_id, persona, seat_no in (
        ("npc_raqio", "raqio", 1),
        ("npc_setsu", "setsu", 2),
        ("npc_gina", "gina", 3),
        ("npc_jonas", "jonas", 4),
    ):
        registry.register(
            npc_id=npc_id,
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[persona]),
            now_ms=1000,
            persona_key=persona,
        )
        registry.assign(npc_id, seat=seat_no, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 2000,
        rng=_random.Random(0),
    )
    await arb.try_dispatch_next(g.id)

    # Either seat 2 or seat 3 wins (random tiebreak), but NOT seat 4
    # (not addressed) and NOT seat 1 (just spoke).
    first_winner: int | None = None
    if bufs["setsu"] and not bufs["gina"]:
        first_winner = 2
    elif bufs["gina"] and not bufs["setsu"]:
        first_winner = 3
    assert first_winner in (2, 3)
    assert not bufs["raqio"], "ラキオ just spoke — never picked"
    assert not bufs["jonas"], "ジョナス wasn't addressed — must lose to addressed pair"


async def test_runoff_dispatch_picks_tied_llm_candidates_in_order(
    repo: SqliteRepo,
) -> None:
    """In DAY_RUNOFF_SPEECH the arbiter ignores the regular speech_count
    rotation and dispatches to **tied** LLM candidates, in seat-no order,
    one at a time. After each accepted SpeakResult, ``runoff_speech_done``
    is flipped so the engine's `plan_runoff_speech_to_runoff` can advance
    once the last tied candidate finishes. Reproduces the production bug
    where Master's rounds-mode batch silently ran in reactive_voice mode
    and TTS never played.
    """
    from wolfbot.domain.models import Vote

    g = Game(
        id="rv-runoff",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_RUNOFF_SPEECH,
        day_number=1,
        deadline_epoch=10**12,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    seats = [
        Seat(
            seat_no=1,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
        Seat(
            seat_no=2, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(
            seat_no=3, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    for sn, role in ((1, Role.WEREWOLF), (2, Role.SEER), (3, Role.MEDIUM)):
        await repo.set_player_role(g.id, sn, role)

    # Round-0 votes that produce tied=(1, 2). Voter 3 abstains so the
    # tally is 1=1, 2=1 — clean two-way tie.
    for voter, target in ((1, 2), (2, 1), (3, None)):
        await repo.insert_vote(
            Vote(game_id=g.id, day=1, round=0, voter_seat=voter, target_seat=target, submitted_at=1)
        )

    phase_id = make_phase_id(g.id, 1, Phase.DAY_RUNOFF_SPEECH)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_RUNOFF_SPEECH,
            alive_seat_nos=[1, 2, 3],
            created_at_ms=1,
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
            npc_id=npc_id,
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[persona]),
            now_ms=1000,
            persona_key=persona,
        )
        registry.assign(npc_id, seat=seat_no, game_id=g.id, phase_id=phase_id)

    intros: list[int] = []  # captured runoff_announce calls

    async def _intro(seat: Seat) -> None:
        intros.append(seat.seat_no)

    wakes: list[str] = []

    def _wake(game_id: str) -> None:
        wakes.append(game_id)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 2000,
        runoff_announce=_intro,
        runoff_wake=_wake,
    )

    # First dispatch: seat 1 (lowest tied seat-no).
    await arb.try_dispatch_next(g.id)
    assert intros == [1], "Master must voice the candidate intro before TTS"
    assert bufs["raqio"], "席1 ラキオ must receive the SpeakRequest"
    assert not bufs["setsu"]
    assert not bufs["gina"], "席3 ジナ is not a tied candidate — never picked"

    seat_repr, reason = await _fetch_selection_reason(repo, g.id)
    assert seat_repr == "1"
    assert reason == "runoff_candidate"

    # NPC accepts → engine wakes, runoff_speech_done flips.
    req_msg = next(m for m in bufs["raqio"] if '"speak_request"' in m)
    import json as _json

    req_payload = _json.loads(req_msg)
    request_id = req_payload["request_id"]
    result = SpeakResult(
        ts=2100,
        trace_id="t",
        request_id=request_id,
        npc_id="npc_raqio",
        phase_id=phase_id,
        status="accepted",
        text="私は皆さんの推理に矛盾を感じています。投票先を見直してほしいです。",
    )
    ok, _ = await arb.handle_speak_result(
        result,
        current_phase_id=phase_id,
        day=1,
        phase=Phase.DAY_RUNOFF_SPEECH,
    )
    assert ok
    progress_seat1 = await repo.load_llm_speech_progress(g.id, 1, 1)
    assert progress_seat1[4] is True, "runoff_speech_done must flip on accept"
    assert g.id in wakes, "engine must wake so the phase can advance"

    # Simulate playback completion so the serial-speech gate releases —
    # the live wiring kicks try_dispatch_next on playback_finished.
    await arb.handle_playback_finished(
        PlaybackFinished(
            ts=2200,
            trace_id="t",
            request_id=request_id,
            npc_id="npc_raqio",
            started_at_ms=2150,
            finished_at_ms=2200,
        )
    )

    # Second dispatch: seat 2 (next tied seat-no).
    bufs["raqio"].clear()
    bufs["setsu"].clear()
    bufs["gina"].clear()
    intros.clear()
    await arb.try_dispatch_next(g.id)
    assert intros == [2]
    assert bufs["setsu"], "席2 セツ must receive the SpeakRequest second"
    assert not bufs["raqio"], "席1 already done — must not be re-picked"
    assert not bufs["gina"]


async def test_runoff_dispatch_skips_when_seat_already_pending(
    repo: SqliteRepo,
) -> None:
    """Two sequential `try_dispatch_next` invocations on phase entry into
    DAY_RUNOFF_SPEECH (post-PHASE_CHANGE-narration kick + the
    `_on_reactive_phase_enter` callback) must NOT both run the Master
    candidate intro. Between the first call's `dispatch_request` (which
    only adds to `_pending`) and the NPC's SpeakResult arriving (which
    flips `_active_playback`), the gate is open — without the
    re-entrancy guard the second call picks the same chosen seat and
    speaks the intro twice. Regression for the user-reported
    "決選投票で最初の候補者の発言を促す案内が2回読み上げられる" bug.
    """
    from wolfbot.domain.models import Vote

    g = Game(
        id="rv-runoff-double-kick",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_RUNOFF_SPEECH,
        day_number=1,
        deadline_epoch=10**12,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    for seat in (
        Seat(
            seat_no=1,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
        Seat(
            seat_no=2,
            display_name="🌙セツ",
            discord_user_id=None,
            is_llm=True,
            persona_key="setsu",
        ),
        Seat(
            seat_no=3,
            display_name="🟣ジナ",
            discord_user_id=None,
            is_llm=True,
            persona_key="gina",
        ),
    ):
        await repo.insert_seat(g.id, seat)
    for sn, role in ((1, Role.WEREWOLF), (2, Role.SEER), (3, Role.MEDIUM)):
        await repo.set_player_role(g.id, sn, role)
    # Tie: seats 1 and 2 (voter 3 abstains).
    for voter, target in ((1, 2), (2, 1), (3, None)):
        await repo.insert_vote(
            Vote(
                game_id=g.id,
                day=1,
                round=0,
                voter_seat=voter,
                target_seat=target,
                submitted_at=1,
            )
        )

    phase_id = make_phase_id(g.id, 1, Phase.DAY_RUNOFF_SPEECH)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_RUNOFF_SPEECH,
            alive_seat_nos=[1, 2, 3],
            created_at_ms=1,
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
            npc_id=npc_id,
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[persona]),
            now_ms=1000,
            persona_key=persona,
        )
        registry.assign(npc_id, seat=seat_no, game_id=g.id, phase_id=phase_id)

    intros: list[int] = []

    async def _intro(seat: Seat) -> None:
        intros.append(seat.seat_no)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 2000,
        runoff_announce=_intro,
    )

    # Two sequential kicks — mirrors the prod ordering where
    # `_master_narrate`'s post-PHASE_CHANGE kick runs before
    # `_on_reactive_phase_enter` fires from `_dispatch_submissions`.
    # Between them, the first dispatch's SpeakRequest is in
    # `_pending` but no SpeakResult has arrived yet so
    # `_active_playback` is empty.
    await arb.try_dispatch_next(g.id)
    await arb.try_dispatch_next(g.id)

    # The intro must fire exactly once, not twice. Seat 1 (lowest
    # tied seat-no) is the chosen candidate; the second kick sees
    # the pending SpeakRequest and bails out.
    assert intros == [1], (
        "candidate intro must be voiced exactly once even on a "
        f"double phase-entry kick (got intros={intros})"
    )
    # Exactly one SpeakRequest was emitted.
    raqio_speak_requests = [m for m in bufs["raqio"] if '"speak_request"' in m]
    assert len(raqio_speak_requests) == 1, (
        "exactly one SpeakRequest must be emitted to the chosen NPC "
        f"(got {len(raqio_speak_requests)})"
    )
    # The other tied seat (席2 セツ) must not be picked while seat 1
    # is still in flight.
    assert not any('"speak_request"' in m for m in bufs["setsu"])


async def test_runoff_dispatch_marks_done_on_offline_npc(
    repo: SqliteRepo,
) -> None:
    """A tied candidate whose NPC bot is offline must be marked done so
    the phase advances rather than stalling on a permanently-silent seat.
    """
    from wolfbot.domain.models import Vote

    g = Game(
        id="rv-runoff-offline",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_RUNOFF_SPEECH,
        day_number=1,
        deadline_epoch=10**12,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    for seat in (
        Seat(
            seat_no=1,
            display_name="🦋ラキオ",
            discord_user_id=None,
            is_llm=True,
            persona_key="raqio",
        ),
        Seat(
            seat_no=2, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(
            seat_no=3, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
    ):
        await repo.insert_seat(g.id, seat)
    for sn, role in ((1, Role.WEREWOLF), (2, Role.SEER), (3, Role.MEDIUM)):
        await repo.set_player_role(g.id, sn, role)
    # Tie: seats 1 and 2 (voter 3 abstains).
    for voter, target in ((1, 2), (2, 1), (3, None)):
        await repo.insert_vote(
            Vote(game_id=g.id, day=1, round=0, voter_seat=voter, target_seat=target, submitted_at=1)
        )

    phase_id = make_phase_id(g.id, 1, Phase.DAY_RUNOFF_SPEECH)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_RUNOFF_SPEECH,
            alive_seat_nos=[1, 2, 3],
            created_at_ms=1,
        )
    )

    # ONLY seat 2's NPC bot is online. Seat 1 (also tied) has no NPC.
    registry = InMemoryNpcRegistry()
    buf2: list[str] = []
    registry.register(
        npc_id="npc_setsu",
        discord_bot_user_id="bot_setsu",
        supported_voices=(),
        version="1",
        send=_captured_send(buf2),
        now_ms=1000,
        persona_key="setsu",
    )
    registry.assign("npc_setsu", seat=2, game_id=g.id, phase_id=phase_id)

    intros: list[int] = []

    async def _intro(seat: Seat) -> None:
        intros.append(seat.seat_no)

    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 2000,
        runoff_announce=_intro,
    )

    await arb.try_dispatch_next(g.id)

    # Seat 1 had no online NPC → marked done. Seat 2 is online → got
    # the SpeakRequest with intro voiced.
    progress_seat1 = await repo.load_llm_speech_progress(g.id, 1, 1)
    assert progress_seat1[4] is True, "offline candidate must be marked done"
    assert intros == [2]
    assert buf2, "online tied candidate must receive the SpeakRequest"


async def test_runoff_watchdog_does_not_pop_pending_after_speak_result_accepted(
    repo: SqliteRepo,
) -> None:
    """Regression for game d57c5d83ed4a day 2: ジョナス returned a SpeakResult
    in 5s but her TTS playback ran 11.6s — longer than the 8s watchdog TTL.
    The watchdog spuriously popped _pending while playback was still active.
    Then `_on_playback_finished` lost the game_id lookup (it depends on
    _pending) and never called try_dispatch_next, so シゲミチ (the other
    tied candidate) was never dispatched and the runoff stalled forever.

    Fix: watchdog must early-exit when the request is in _active_playback
    (= SpeakResult was accepted; playback handler will re-dispatch on finish).
    """
    import asyncio

    from wolfbot.domain.models import Vote

    g = Game(
        id="rv-runoff-watchdog",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_RUNOFF_SPEECH,
        day_number=1,
        deadline_epoch=10**12,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    for seat in (
        Seat(
            seat_no=1,
            display_name="🎩ジョナス",
            discord_user_id=None,
            is_llm=True,
            persona_key="jonas",
        ),
        Seat(
            seat_no=6,
            display_name="👽シゲミチ",
            discord_user_id=None,
            is_llm=True,
            persona_key="shigemichi",
        ),
        Seat(
            seat_no=2, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"
        ),
    ):
        await repo.insert_seat(g.id, seat)
    for sn, role in ((1, Role.WEREWOLF), (6, Role.VILLAGER), (2, Role.KNIGHT)):
        await repo.set_player_role(g.id, sn, role)
    # Tie between 1 and 6.
    for voter, target in ((1, 6), (6, 1), (2, None)):
        await repo.insert_vote(
            Vote(game_id=g.id, day=1, round=0, voter_seat=voter, target_seat=target, submitted_at=1)
        )

    phase_id = make_phase_id(g.id, 1, Phase.DAY_RUNOFF_SPEECH)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=1,
            phase=Phase.DAY_RUNOFF_SPEECH,
            alive_seat_nos=[1, 2, 6],
            created_at_ms=1,
        )
    )

    registry = InMemoryNpcRegistry()
    bufs: dict[str, list[str]] = {"jonas": [], "shigemichi": []}
    for npc_id, persona, seat_no in (
        ("npc_jonas", "jonas", 1),
        ("npc_shigemichi", "shigemichi", 6),
    ):
        registry.register(
            npc_id=npc_id,
            discord_bot_user_id=f"bot_{persona}",
            supported_voices=(),
            version="1",
            send=_captured_send(bufs[persona]),
            now_ms=1000,
            persona_key=persona,
        )
        registry.assign(npc_id, seat=seat_no, game_id=g.id, phase_id=phase_id)

    intros: list[int] = []

    async def _intro(seat: Seat) -> None:
        intros.append(seat.seat_no)

    # Very short TTL so the watchdog fires within the test window without
    # asyncio.sleep(8). 50ms is enough to let _watchdog wake up.
    cfg = SpeakArbiterConfig(request_ttl_ms=50)
    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 2000,
        runoff_announce=_intro,
        config=cfg,
    )

    # Step 1: dispatch first candidate (ジョナス).
    await arb.try_dispatch_next(g.id)
    assert intros == [1]
    assert bufs["jonas"], "first tied candidate must receive SpeakRequest"

    req_msg = next(m for m in bufs["jonas"] if '"speak_request"' in m)
    import json as _json

    request_id = _json.loads(req_msg)["request_id"]

    # Step 2: NPC accepts SpeakResult (adds to _active_playback, leaves
    # _pending populated until playback finishes).
    result = SpeakResult(
        ts=2050,
        trace_id="t",
        request_id=request_id,
        npc_id="npc_jonas",
        phase_id=phase_id,
        status="accepted",
        text="諸君……ラキオの占い師主張など、笑止千万！",
    )
    ok, _ = await arb.handle_speak_result(
        result,
        current_phase_id=phase_id,
        day=1,
        phase=Phase.DAY_RUNOFF_SPEECH,
    )
    assert ok
    assert request_id in arb._active_playback
    assert request_id in arb._pending  # not popped yet — playback ongoing

    # Step 3: wait long enough for the watchdog to wake (TTL=50ms +
    # scheduling slack). The fix's early-exit guard must prevent the
    # watchdog from popping _pending.
    await asyncio.sleep(0.2)

    assert request_id in arb._pending, (
        "watchdog must NOT pop _pending while SpeakResult is still in playback. "
        "If this fails, _on_playback_finished will lose game_id lookup and "
        "the runoff will stall (see game d57c5d83ed4a day 2)."
    )

    # Step 4: simulate playback completion. _pending lookup must still work,
    # so the next candidate gets dispatched.
    bufs["jonas"].clear()
    bufs["shigemichi"].clear()
    intros.clear()
    await arb.handle_playback_finished(
        PlaybackFinished(
            ts=2200,
            trace_id="t",
            request_id=request_id,
            npc_id="npc_jonas",
            started_at_ms=2050,
            finished_at_ms=2200,
        )
    )
    # In production this is wrapped by main.py's _on_playback_finished
    # which calls try_dispatch_next — simulate that here.
    await arb.try_dispatch_next(g.id)
    assert intros == [6], "second tied candidate must be dispatched after first finishes"
    assert bufs["shigemichi"], "席6 シゲミチ must receive the second SpeakRequest"


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
        seat_names={1: "Alice", 2: "Bob", 3: "Carol"},
    )
    assert packet.recipient_npc_id == "npc_p3"
    assert packet.expires_at_ms == 2000
    assert any(c.id == "co-2-seer" for c in packet.logic_candidates)
    # Naming policy: summary renders by display_name when ``seat_names``
    # is supplied (production wiring always supplies it).
    assert "Bob=seer" in packet.public_state_summary
    assert "silent_seats=[Carol]" in packet.public_state_summary
    # Without `seat_names` (legacy / unit-test fallback) the summary
    # falls back to 席N — covered by the next test.


def test_logic_packet_summary_falls_back_to_seat_when_no_names_supplied() -> None:
    state = PublicDiscussionState(
        game_id="g",
        phase_id="g::day1::DAY_DISCUSSION::1",
        day=1,
        alive_seat_nos=frozenset({1, 2, 3}),
    )
    from wolfbot.domain.discussion import CoClaim

    state.co_claims = (CoClaim(seat=2, role_claim="seer", declared_at_event_id="e1"),)
    state.silent_seats = frozenset({3})
    packet = build_logic_packet(
        state=state,
        recipient_npc_id="npc_p3",
        expires_at_ms=2000,
        now_ms=1500,
    )
    # No seat_names → legacy 席N rendering preserved for back-compat.
    assert "席2=seer" in packet.public_state_summary
    assert "silent_seats=[席3]" in packet.public_state_summary


# ─── fabrication-retry path ──────────────────────────────────────────


async def _seed_seer_with_divine(repo: SqliteRepo) -> Game:
    """Seed a game where seat 2 is real seer with NIGHT_0 random white on seat 1.

    Seat 1 is villager so seat 2's NIGHT_0 random white pointing at seat 1
    is internally consistent (the system never picks a wolf for the random
    white). We seed the result via a ``SEER_RESULT_NIGHT0`` row in
    ``logs_private`` so the validator's ``_load_actual_seer_history``
    (which goes through ``load_private_state_for_seat``) picks it up the
    same way the NPC's ``自分の占い結果`` prompt section does.

    The earlier version of this helper inserted into ``night_actions``,
    which the NPC's prompt builder ignores for NIGHT_0; the validator
    then read truth from a different source than the NPC and bounced
    every legitimate Setsu claim. Game ``6a0dd72d63e3`` reproduced the
    bug: real seer Setsu hit the 5-retry cap on her FIRST legitimate
    Jonas白 claim because the validator couldn't find Jonas in her
    actual history.
    """
    from wolfbot.domain.models import LogEntry

    g = Game(
        id="rv_fab",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
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
    # Seat 1 must NOT be a wolf — NIGHT_0 random white only ever picks
    # non-wolf targets, so this keeps the seed internally consistent
    # with the seer's recorded result.
    await repo.set_player_role(g.id, 1, Role.VILLAGER)
    await repo.set_player_role(g.id, 2, Role.SEER)
    # NIGHT_0 random white on seat 1 (Alice). Color = white (= is_wolf False).
    # The text format must match `private_state._SEER_NIGHT0_RE` so the
    # validator's `load_private_state_for_seat` parses it correctly.
    await repo.insert_log_private(LogEntry(
        game_id=g.id,
        day=0,
        phase=Phase.NIGHT_0,
        kind="SEER_RESULT_NIGHT0",
        actor_seat=2,
        visibility="PRIVATE",
        audience_seat=2,
        text="初日ランダム白: Alice は 人狼ではありません。",
        created_at=0,
    ))
    # Seed phase_baseline so rebuild_public_state returns non-None.
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=make_phase_id(g.id, 1, Phase.DAY_DISCUSSION),
            day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=(1, 2),
            created_at_ms=900,
        )
    )
    return g


async def test_real_seer_legal_claim_passes(repo: SqliteRepo) -> None:
    """Real seer claiming the actual NIGHT_0 target+color is accepted."""
    from wolfbot.domain.ws_messages import ClaimedSeerResult

    g = await _seed_seer_with_divine(repo)
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
    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 1500,
    )
    state = _seed_state(g.id)
    req, _ = await arb.dispatch_request(
        state=state,
        candidate_npc_id="npc_p2",
        seat_no=2,
        game_id=g.id,
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
        text="私は占い師。Aliceを占ったら白。",
        co_declaration="seer",
        # Real seer's NIGHT_0 random white is on seat 1 (Alice, villager).
        # Matches the seeded SEER_RESULT_NIGHT0 entry exactly.
        claimed_seer_result=ClaimedSeerResult(target_seat=1, is_wolf=False),
    )
    ok, reason = await arb.handle_speak_result(
        result,
        current_phase_id=req.phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
    )
    assert ok and reason is None, f"expected accept, got reason={reason}"
    # PlaybackAuthorized should be on the back-channel.
    assert any('"playback_authorized"' in m for m in npc_buf)


async def test_real_seer_fabricated_target_triggers_retry(
    repo: SqliteRepo,
) -> None:
    """Real seer claiming an unrecorded target gets PlaybackRejected and
    is re-dispatched to the same NPC with retry_feedback embedded."""
    from wolfbot.domain.ws_messages import ClaimedSeerResult, SpeakRequest

    g = await _seed_seer_with_divine(repo)
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
    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 1500,
    )
    state = _seed_state(g.id)
    req, _ = await arb.dispatch_request(
        state=state,
        candidate_npc_id="npc_p2",
        seat_no=2,
        game_id=g.id,
    )
    assert req is not None
    npc_buf.clear()
    # The seeded NIGHT_0 result is `target=1, is_wolf=False`. Flipping
    # the verdict to is_wolf=True for the same target triggers
    # REASON_SEER_WRONG_VERDICT (one of FABRICATION_REASONS).
    bad = SpeakResult(
        ts=1600,
        trace_id="t",
        request_id=req.request_id,
        npc_id="npc_p2",
        phase_id=req.phase_id,
        status="accepted",
        text="Aliceを占ったら黒だった",
        co_declaration="seer",
        claimed_seer_result=ClaimedSeerResult(target_seat=1, is_wolf=True),
    )
    ok, reason = await arb.handle_speak_result(
        bad,
        current_phase_id=req.phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
    )
    assert not ok
    assert reason == "fabricated_seer_verdict"
    # 1) PlaybackRejected was sent.
    rejections = [m for m in npc_buf if '"playback_rejected"' in m]
    assert len(rejections) == 1
    rej = PlaybackRejected.model_validate_json(rejections[0])
    assert rej.failure_reason == "fabricated_seer_verdict"
    # 2) A new SpeakRequest was dispatched to the same NPC.
    speak_requests = [m for m in npc_buf if '"speak_request"' in m]
    assert len(speak_requests) == 1
    retry = SpeakRequest.model_validate_json(speak_requests[0])
    assert retry.npc_id == "npc_p2"
    assert retry.seat_no == 2
    # 3) retry_feedback non-empty and references the correction.
    assert retry.retry_feedback is not None
    assert "claimed_seer_result" in retry.retry_feedback


async def test_fabrication_retries_capped_then_falls_back(
    repo: SqliteRepo,
) -> None:
    """After 5 consecutive fabrications the same NPC is no longer
    re-dispatched — the rejection becomes a normal _reject_and_advance."""
    from wolfbot.domain.ws_messages import ClaimedSeerResult

    g = await _seed_seer_with_divine(repo)
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
    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 1500,
    )
    state = _seed_state(g.id)
    # Wrong verdict for the seeded target (real history has is_wolf=False).
    bad_claim = ClaimedSeerResult(target_seat=1, is_wolf=True)
    last_reason = None
    last_phase_id = None
    # 5 attempts of fabrication. Each loop dispatches a fresh request,
    # then feeds a fabricated SpeakResult back to the arbiter.
    for i in range(5):
        req, _ = await arb.dispatch_request(
            state=state,
            candidate_npc_id="npc_p2",
            seat_no=2,
            game_id=g.id,
        )
        assert req is not None, f"dispatch failed at attempt {i}"
        last_phase_id = req.phase_id
        bad = SpeakResult(
            ts=1600 + i,
            trace_id="t",
            request_id=req.request_id,
            npc_id="npc_p2",
            phase_id=req.phase_id,
            status="accepted",
            text=f"attempt {i} bogus claim",
            co_declaration="seer",
            claimed_seer_result=bad_claim,
        )
        ok, reason = await arb.handle_speak_result(
            bad,
            current_phase_id=req.phase_id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
        )
        assert not ok
        last_reason = reason
    # By now _fabrication_retries[(g.id, phase_id, npc_p2)] == 5; cap is 5
    # so the next fabrication should bail to normal rotation (no retry
    # dispatch from `_reject_and_retry_same_npc`).
    npc_buf.clear()
    # After 5 attempts the counter reached the cap. The arbiter no
    # longer re-dispatches the same NPC; the rejection path went through
    # `_reject_and_advance` (which calls try_dispatch_next, but with
    # only 1 NPC online and that NPC just declined, nothing new dispatches).
    assert last_reason == "fabricated_seer_verdict"
    counter = arb._fabrication_retries.get(  # type: ignore[attr-defined]
        (g.id, last_phase_id, "npc_p2")
    )
    assert counter == 5, f"expected 5 fabrications recorded, got {counter}"


async def test_accept_resets_fabrication_counter(repo: SqliteRepo) -> None:
    """A successful accept clears the per-(game, phase, npc) counter so
    the cap doesn't carry over from earlier fabrications."""
    from wolfbot.domain.ws_messages import ClaimedSeerResult

    g = await _seed_seer_with_divine(repo)
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
    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 1500,
    )
    state = _seed_state(g.id)
    # Attempt 1: fabrication.
    req1, _ = await arb.dispatch_request(
        state=state,
        candidate_npc_id="npc_p2",
        seat_no=2,
        game_id=g.id,
    )
    assert req1 is not None
    bad = SpeakResult(
        ts=1600,
        trace_id="t",
        request_id=req1.request_id,
        npc_id="npc_p2",
        phase_id=req1.phase_id,
        status="accepted",
        text="間違い",
        co_declaration="seer",
        # Wrong verdict (truth is is_wolf=False on seat 1).
        claimed_seer_result=ClaimedSeerResult(target_seat=1, is_wolf=True),
    )
    await arb.handle_speak_result(
        bad,
        current_phase_id=req1.phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
    )
    key = (g.id, req1.phase_id, "npc_p2")
    assert arb._fabrication_retries.get(key) == 1  # type: ignore[attr-defined]
    # The retry path issued a new SpeakRequest. Pick it up via npc_buf.
    speak_reqs = [m for m in npc_buf if '"speak_request"' in m]
    from wolfbot.domain.ws_messages import SpeakRequest as SR

    retry_req = SR.model_validate_json(speak_reqs[-1])
    # Attempt 2: correct claim (matches seeded NIGHT_0 white).
    good = SpeakResult(
        ts=1700,
        trace_id="t",
        request_id=retry_req.request_id,
        npc_id="npc_p2",
        phase_id=retry_req.phase_id,
        status="accepted",
        text="Aliceは白だった",
        co_declaration="seer",
        claimed_seer_result=ClaimedSeerResult(target_seat=1, is_wolf=False),
    )
    ok, _ = await arb.handle_speak_result(
        good,
        current_phase_id=retry_req.phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
    )
    assert ok
    # Counter cleared.
    assert key not in arb._fabrication_retries  # type: ignore[attr-defined]


async def test_fake_seer_target_swap_triggers_retry(repo: SqliteRepo) -> None:
    """Fake seer (wolf) who claimed Bob白 then tries to claim Alice白 in
    the same morning gets rejected with retry feedback."""
    from wolfbot.domain.ws_messages import ClaimedSeerResult, SpeakRequest

    g = Game(
        id="rv_fake",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=2,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    seats = [
        Seat(seat_no=1, display_name="Alice", discord_user_id="u1", is_llm=False, persona_key=None),
        Seat(
            seat_no=2, display_name="セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(
            seat_no=3,
            display_name="ユリコ",
            discord_user_id=None,
            is_llm=True,
            persona_key="yuriko",
        ),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.VILLAGER)
    await repo.set_player_role(g.id, 2, Role.SEER)
    await repo.set_player_role(g.id, 3, Role.WEREWOLF)
    # Seed a prior public claim from seat 3 (wolf) on day 2: target=1 white.
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    phase_id = make_phase_id(g.id, 2, Phase.DAY_DISCUSSION)
    await store.insert(
        make_phase_baseline(
            game_id=g.id,
            phase_id=phase_id,
            day=2,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=(1, 2, 3),
            created_at_ms=900,
        )
    )
    from wolfbot.domain.discussion import (
        SpeakerKind as SK,
    )
    from wolfbot.domain.discussion import (
        SpeechEvent as SE,
    )
    from wolfbot.domain.discussion import (
        SpeechSource as SS,
    )

    await store.insert(
        SE(
            event_id="e_prior_claim",
            game_id=g.id,
            phase_id=phase_id,
            day=2,
            phase=Phase.DAY_DISCUSSION,
            source=SS.NPC_GENERATED,
            speaker_kind=SK.NPC,
            speaker_seat=3,
            text="私が占い師。Alice白",
            co_declaration="seer",
            claimed_seer_target_seat=1,
            claimed_seer_is_wolf=False,
            created_at_ms=1000,
        )
    )
    registry = InMemoryNpcRegistry()
    npc_buf: list[str] = []
    registry.register(
        npc_id="npc_p3",
        discord_bot_user_id="bot3",
        supported_voices=(),
        version="1",
        send=_captured_send(npc_buf),
        now_ms=1000,
        persona_key="yuriko",
    )
    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 1500,
    )
    state = PublicDiscussionState(
        game_id=g.id,
        phase_id=phase_id,
        day=2,
        alive_seat_nos=frozenset({1, 2, 3}),
        silent_seats=frozenset({1, 2}),
    )
    req, _ = await arb.dispatch_request(
        state=state,
        candidate_npc_id="npc_p3",
        seat_no=3,
        game_id=g.id,
    )
    assert req is not None
    npc_buf.clear()
    # Wolf tries to swap day-2 target to seat 2.
    swap = SpeakResult(
        ts=1600,
        trace_id="t",
        request_id=req.request_id,
        npc_id="npc_p3",
        phase_id=req.phase_id,
        status="accepted",
        text="セツを占ったら白",
        co_declaration="seer",
        claimed_seer_result=ClaimedSeerResult(target_seat=2, is_wolf=False),
    )
    ok, reason = await arb.handle_speak_result(
        swap,
        current_phase_id=req.phase_id,
        day=2,
        phase=Phase.DAY_DISCUSSION,
    )
    assert not ok
    assert reason == "seer_target_swap"
    # Retry was dispatched.
    speak_reqs = [m for m in npc_buf if '"speak_request"' in m]
    assert len(speak_reqs) == 1
    retry = SpeakRequest.model_validate_json(speak_reqs[0])
    assert retry.npc_id == "npc_p3"
    assert retry.retry_feedback is not None and "席1" in retry.retry_feedback


async def test_null_claim_passes_validator(repo: SqliteRepo) -> None:
    """Utterance with claimed_*_result=None bypasses validation entirely
    even from the real seer (general talk should never be rejected)."""
    g = await _seed_seer_with_divine(repo)
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
    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=discussion,
        now_ms=lambda: 1500,
    )
    state = _seed_state(g.id)
    req, _ = await arb.dispatch_request(
        state=state,
        candidate_npc_id="npc_p2",
        seat_no=2,
        game_id=g.id,
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
        result,
        current_phase_id=req.phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
    )
    assert ok and reason is None


async def test_fabrication_cap_blocks_seat_from_re_selection(
    repo: SqliteRepo,
) -> None:
    """After a seat hits ``_MAX_FABRICATION_RETRIES``, the picker must
    NOT re-select that seat for the rest of the phase. Without this
    block, normal rotation re-picks the same NPC immediately (especially
    when they're in the seer-callout pool), the retry counter increments
    past the cap (observed in game ``6366cb014a0a``: attempt=6/5, 7/5,
    8/5, 9/5...), and the phase stalls because no other NPC ever gets
    dispatched.

    Setup: 2 LLM seats online, both online for the same phase. NPC A
    fabricates 5 times in a row → cap fires → A is blocked. The
    arbiter's ``try_dispatch_next`` should now pick NPC B.
    """
    from wolfbot.domain.ws_messages import ClaimedSeerResult

    g = await _seed_seer_with_divine(repo)
    # Add a 3rd LLM seat and overwrite the phase_baseline so the alive
    # set in `rebuild_public_state` includes seat 3 as a dispatchable
    # candidate. The fold uses the FIRST baseline it sees, so we must
    # delete the original (alive=(1,2)) before inserting the new one.
    third = Seat(
        seat_no=3, display_name="Charlie",
        discord_user_id=None, is_llm=True, persona_key="raqio",
    )
    await repo.insert_seat(g.id, third)
    await repo.set_player_role(g.id, 3, Role.VILLAGER)
    await repo._conn.execute(  # type: ignore[attr-defined]
        "DELETE FROM speech_events WHERE game_id=? AND source='phase_baseline'",
        (g.id,),
    )
    await repo._conn.commit()  # type: ignore[attr-defined]
    store_extra = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    await store_extra.insert(make_phase_baseline(
        game_id=g.id,
        phase_id=make_phase_id(g.id, 1, Phase.DAY_DISCUSSION),
        day=1, phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=(1, 2, 3), created_at_ms=950,
    ))

    registry = InMemoryNpcRegistry()
    a_buf: list[str] = []
    b_buf: list[str] = []
    registry.register(
        npc_id="npc_p2", discord_bot_user_id="bot2", supported_voices=(),
        version="1", send=_captured_send(a_buf), now_ms=1000,
        persona_key="setsu",
    )
    registry.register(
        npc_id="npc_p3", discord_bot_user_id="bot3", supported_voices=(),
        version="1", send=_captured_send(b_buf), now_ms=1000,
        persona_key="raqio",
    )
    registry.assign("npc_p2", seat=2, game_id=g.id,
                    phase_id=make_phase_id(g.id, 1, Phase.DAY_DISCUSSION))
    registry.assign("npc_p3", seat=3, game_id=g.id,
                    phase_id=make_phase_id(g.id, 1, Phase.DAY_DISCUSSION))

    discussion = DiscussionService(
        store=SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    )
    arb = SpeakArbiter(
        repo=repo, registry=registry, discussion=discussion,
        now_ms=lambda: 1500,
    )
    bad_claim = ClaimedSeerResult(target_seat=1, is_wolf=True)

    last_phase_id = None
    # 5 fabrication attempts on seat 2 → cap.
    for i in range(5):
        req, _ = await arb.dispatch_request(
            state=_seed_state(g.id), candidate_npc_id="npc_p2",
            seat_no=2, game_id=g.id,
        )
        assert req is not None
        last_phase_id = req.phase_id
        bad = SpeakResult(
            ts=1600 + i, trace_id="t", request_id=req.request_id,
            npc_id="npc_p2", phase_id=req.phase_id, status="accepted",
            text=f"attempt {i}", co_declaration="seer",
            claimed_seer_result=bad_claim,
        )
        await arb.handle_speak_result(
            bad, current_phase_id=req.phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
        )

    # Seat 2 must now be in the cap-hit set.
    assert (g.id, last_phase_id) in arb._fabrication_capped  # type: ignore[attr-defined]
    capped = arb._fabrication_capped[(g.id, last_phase_id)]  # type: ignore[attr-defined]
    assert 2 in capped
    assert 3 not in capped

    # When the picker runs `try_dispatch_next`, it must skip seat 2
    # and dispatch to seat 3 instead. We exercise this by calling
    # `try_dispatch_next` directly and checking that npc_p3's send buffer
    # received a SpeakRequest while npc_p2's didn't get a fresh one.
    a_buf.clear()
    b_buf.clear()
    await arb.try_dispatch_next(g.id)
    # Seat 2 (capped) should NOT have been dispatched.
    assert not any('"speak_request"' in m for m in a_buf), (
        "capped seat 2 should be skipped, but got: " + str(a_buf)
    )
    # Seat 3 (not capped) should have been dispatched.
    assert any('"speak_request"' in m for m in b_buf), (
        "seat 3 should have been picked, got buf: " + str(b_buf)
    )


async def test_co_cap_exceeded_one_shot_skip_no_retry(repo: SqliteRepo) -> None:
    """A 4th seat trying to seer-CO when the ledger already has 3
    distinct seers gets PlaybackRejected on this dispatch — but the
    fabrication retry counter stays 0 and the seat is NOT blocked
    from future picks. The cap is a structural rule, not a self-
    correction problem; retrying the same NPC with feedback won't
    help (the model would have to abandon CO entirely, which is
    cleaner to enforce by simply not re-dispatching this attempt)."""
    from wolfbot.domain.discussion import (
        SpeakerKind as SK,
    )
    from wolfbot.domain.discussion import (
        SpeechEvent as SE,
    )
    from wolfbot.domain.discussion import (
        SpeechSource as SS,
    )
    from wolfbot.domain.ws_messages import ClaimedSeerResult

    g = Game(
        id="rv_cocap", guild_id="gu", host_user_id="h",
        phase=Phase.DAY_DISCUSSION, day_number=1,
        main_text_channel_id="c1", main_vc_channel_id="c2",
        created_at=0, discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    seats = [
        Seat(seat_no=1, display_name="A", discord_user_id=None, is_llm=True, persona_key="setsu"),
        Seat(seat_no=2, display_name="B", discord_user_id=None, is_llm=True, persona_key="gina"),
        Seat(seat_no=3, display_name="C", discord_user_id=None, is_llm=True, persona_key="raqio"),
        Seat(seat_no=4, display_name="D", discord_user_id=None, is_llm=True, persona_key="comet"),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    for sn, role in [(1, Role.SEER), (2, Role.WEREWOLF), (3, Role.MADMAN), (4, Role.VILLAGER)]:
        await repo.set_player_role(g.id, sn, role)
    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    await store.insert(make_phase_baseline(
        game_id=g.id, phase_id=phase_id, day=1, phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=(1, 2, 3, 4), created_at_ms=900,
    ))
    # Seed 3 prior seer COs in speech_events from seats 1, 2, 3.
    for i, sn in enumerate([1, 2, 3]):
        await store.insert(SE(
            event_id=f"e_co_{sn}", game_id=g.id, phase_id=phase_id,
            day=1, phase=Phase.DAY_DISCUSSION,
            source=SS.NPC_GENERATED, speaker_kind=SK.NPC,
            speaker_seat=sn, text=f"占い師CO from seat {sn}",
            co_declaration="seer",
            claimed_seer_target_seat=sn % 4 + 1,
            claimed_seer_is_wolf=False,
            created_at_ms=1000 + i,
        ))
    registry = InMemoryNpcRegistry()
    npc4_buf: list[str] = []
    registry.register(
        npc_id="npc_p4", discord_bot_user_id="bot4", supported_voices=(),
        version="1", send=_captured_send(npc4_buf), now_ms=1000,
        persona_key="comet",
    )
    registry.assign("npc_p4", seat=4, game_id=g.id, phase_id=phase_id)
    discussion = DiscussionService(store=store)
    arb = SpeakArbiter(repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 1500)

    state = PublicDiscussionState(
        game_id=g.id, phase_id=phase_id, day=1,
        alive_seat_nos=frozenset({1, 2, 3, 4}),
        silent_seats=frozenset({4}),
    )
    req, _ = await arb.dispatch_request(
        state=state, candidate_npc_id="npc_p4", seat_no=4, game_id=g.id,
    )
    assert req is not None
    npc4_buf.clear()

    # Seat 4 (villager) tries to CO seer despite 3 already out.
    bad = SpeakResult(
        ts=1600, trace_id="t", request_id=req.request_id,
        npc_id="npc_p4", phase_id=req.phase_id, status="accepted",
        text="僕も占い師だよ", co_declaration="seer",
        claimed_seer_result=ClaimedSeerResult(target_seat=1, is_wolf=False),
    )
    ok, reason = await arb.handle_speak_result(
        bad, current_phase_id=req.phase_id, day=1,
        phase=Phase.DAY_DISCUSSION,
    )
    assert not ok
    assert reason == "seer_co_cap_exceeded"
    # PlaybackRejected was sent.
    assert any('"playback_rejected"' in m for m in npc4_buf)
    # Fabrication retry counter stayed 0 (cap is NOT a fabrication).
    assert (g.id, req.phase_id, "npc_p4") not in arb._fabrication_retries  # type: ignore[attr-defined]
    # Seat is NOT in the cap-block set (= remains eligible for future picks).
    assert 4 not in arb._fabrication_capped.get((g.id, req.phase_id), set())  # type: ignore[attr-defined]
