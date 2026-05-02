"""Bundle 4: master speech control surface.

Exercises:
- WebSocket envelope dispatch with the canonical `type` field.
- NPC `npc_register` → `npc_registered` handshake updates the registry and
  replies on the back-channel.
- Heartbeats refresh `last_heartbeat_ms` and revive an offline NPC.
- Voice-ingest connections receive a `registry_snapshot` on attach and a
  `registry_update` on subsequent NPC registration deltas.
- `MasterIngestService` discards STT payloads whose Discord user id matches
  a registered NPC bot.
- The new audit tables (`npc_speak_requests` / `npc_speak_results` /
  `npc_playback_events`) round-trip through the repo helpers.

No real WebSocket server is started — `HandlerRegistry.dispatch` is invoked
directly with synthetic JSON envelopes.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import pytest

from wolfbot.domain.discussion import SpeechSource, make_phase_id
from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Seat
from wolfbot.domain.ws_messages import (
    Heartbeat,
    NpcRegister,
    NpcRegistered,
    RegistrySnapshot,
    RegistryUpdate,
    SpeakResult,
    SpeechEventPayload,
)
from wolfbot.master.arbiter.ingest_service import (
    MasterIngestService,
    PhaseLookup,
)
from wolfbot.master.ws.npc_registry import InMemoryNpcRegistry
from wolfbot.master.ws.ws_server import (
    ConnectionContext,
    HandlerRegistry,
    MasterHandlers,
)
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discussion_service import (
    DiscussionService,
    SqliteSpeechEventStore,
)


def _make_send_capture() -> tuple[list[str], Callable[[str], Awaitable[None]]]:
    """Returns (captured, send) — `send(msg)` appends to `captured`."""
    captured: list[str] = []

    async def send(msg: str) -> None:
        captured.append(msg)

    return captured, send


def _make_npc_ctx(*, tag: str = "npc-temp") -> tuple[ConnectionContext, list[str]]:
    captured, send = _make_send_capture()
    return ConnectionContext(role="npc", tag=tag, send=send), captured


def _make_voice_ingest_ctx() -> tuple[ConnectionContext, list[str]]:
    captured, send = _make_send_capture()
    return (
        ConnectionContext(role="voice-ingest", tag="voice-ingest", send=send),
        captured,
    )


# ---------------------------------------------------------------- registration


async def test_npc_register_responds_with_npc_registered_and_inserts_registry() -> None:
    registry = InMemoryNpcRegistry()
    handlers = MasterHandlers(registry=registry, now_ms=lambda: 1000)
    dispatcher = HandlerRegistry()
    handlers.install(dispatcher)
    ctx, captured = _make_npc_ctx()
    msg = NpcRegister(
        ts=999,
        trace_id="t-1",
        npc_id="npc_p5",
        discord_bot_user_id="bot-uid-5",
        supported_voices=("ja-JP-A",),
        version="1.0.0", persona_key="setsu")
    await dispatcher.dispatch(msg.model_dump_json(), ctx)

    assert ctx.tag == "npc_p5"
    entry = registry.get("npc_p5")
    assert entry is not None
    assert entry.discord_bot_user_id == "bot-uid-5"
    assert entry.last_heartbeat_ms == 1000
    assert entry.is_online is True
    assert len(captured) == 1
    reply = NpcRegistered.model_validate_json(captured[0])
    assert reply.npc_id == "npc_p5"
    assert reply.assigned_seat is None  # not assigned yet


async def test_heartbeat_refreshes_last_heartbeat_and_revives_offline_npc() -> None:
    registry = InMemoryNpcRegistry()
    times = iter([1000, 2000, 9000])
    handlers = MasterHandlers(registry=registry, now_ms=lambda: next(times))
    dispatcher = HandlerRegistry()
    handlers.install(dispatcher)
    ctx, _ = _make_npc_ctx()

    await dispatcher.dispatch(
        NpcRegister(
            ts=900, trace_id="t", npc_id="npc_p5", discord_bot_user_id="b5", persona_key="setsu").model_dump_json(),
        ctx,
    )
    # Force offline.
    registry.prune_offline(now_ms=10_000, timeout_ms=1)
    entry = registry.get("npc_p5")
    assert entry is not None and entry.is_online is False

    # Heartbeat with a fresh ts re-flips online.
    await dispatcher.dispatch(
        Heartbeat(ts=8000, trace_id="t", npc_id="npc_p5").model_dump_json(),
        ctx,
    )
    entry = registry.get("npc_p5")
    assert entry is not None and entry.is_online is True
    assert entry.last_heartbeat_ms == 9000


async def test_unknown_message_type_is_logged_not_raised() -> None:
    registry = InMemoryNpcRegistry()
    handlers = MasterHandlers(registry=registry, now_ms=lambda: 1)
    dispatcher = HandlerRegistry()
    handlers.install(dispatcher)
    ctx, _ = _make_npc_ctx()
    # Should not raise.
    await dispatcher.dispatch(
        json.dumps({"type": "definitely_not_a_real_type", "ts": 1, "trace_id": "x"}),
        ctx,
    )


async def test_invalid_json_is_logged_not_raised() -> None:
    registry = InMemoryNpcRegistry()
    handlers = MasterHandlers(registry=registry, now_ms=lambda: 1)
    dispatcher = HandlerRegistry()
    handlers.install(dispatcher)
    ctx, _ = _make_npc_ctx()
    await dispatcher.dispatch("{not-json", ctx)


async def test_speak_result_dispatches_to_handler() -> None:
    registry = InMemoryNpcRegistry()
    received: list[SpeakResult] = []

    async def on_result(msg: SpeakResult, _ctx: ConnectionContext) -> None:
        received.append(msg)

    handlers = MasterHandlers(registry=registry, on_speak_result=on_result, now_ms=lambda: 1)
    dispatcher = HandlerRegistry()
    handlers.install(dispatcher)
    ctx, _ = _make_npc_ctx(tag="npc_p1")
    msg = SpeakResult(
        ts=1,
        trace_id="t",
        request_id="r1",
        npc_id="npc_p1",
        phase_id="p",
        status="accepted",
        text="やあ",
    )
    await dispatcher.dispatch(msg.model_dump_json(), ctx)
    assert len(received) == 1
    assert received[0].request_id == "r1"


# ---------------------------------------------------------------- registry listener


async def test_registry_listener_emits_added_when_new_npc_registers() -> None:
    registry = InMemoryNpcRegistry()
    deltas: list[tuple[set[str], set[str]]] = []

    async def listener(added: set[str], removed: set[str]) -> None:
        deltas.append((added, removed))

    registry.add_listener(listener)
    handlers = MasterHandlers(registry=registry, now_ms=lambda: 1)
    dispatcher = HandlerRegistry()
    handlers.install(dispatcher)
    ctx, _ = _make_npc_ctx()
    await dispatcher.dispatch(
        NpcRegister(
            ts=1, trace_id="t", npc_id="npc_a", discord_bot_user_id="botA", persona_key="setsu").model_dump_json(),
        ctx,
    )
    # Allow the scheduled listener task to run.
    import asyncio

    await asyncio.sleep(0)
    assert deltas == [({"botA"}, set())]


async def test_registry_unregister_emits_removed() -> None:
    registry = InMemoryNpcRegistry()
    deltas: list[tuple[set[str], set[str]]] = []

    async def listener(added: set[str], removed: set[str]) -> None:
        deltas.append((added, removed))

    registry.register(
        npc_id="npc_b",
        discord_bot_user_id="botB",
        supported_voices=(),
        version="0.0.1",
        send=None,
        now_ms=1, persona_key="setsu")
    registry.add_listener(listener)
    registry.unregister("npc_b", reason="ws_closed")
    import asyncio

    await asyncio.sleep(0)
    assert deltas == [(set(), {"botB"})]


async def test_registry_snapshot_message_serializable_round_trip() -> None:
    msg = RegistrySnapshot(ts=1, trace_id="t", npc_user_ids=("a", "b"))
    out = RegistrySnapshot.model_validate_json(msg.model_dump_json())
    assert out.npc_user_ids == ("a", "b")


async def test_registry_update_message_serializable_round_trip() -> None:
    msg = RegistryUpdate(ts=1, trace_id="t", added=("x",), removed=("y",))
    out = RegistryUpdate.model_validate_json(msg.model_dump_json())
    assert out.added == ("x",) and out.removed == ("y",)


# ---------------------------------------------------------------- ingest service


class _StubPhaseLookup:
    def __init__(
        self,
        mapping: dict[str, tuple[Phase, int]],
        alive_seats: dict[str, list[int]] | None = None,
        addressed: dict[tuple[str, str], int] | None = None,
    ) -> None:
        self._mapping = mapping
        self._alive = alive_seats or {}
        self._addressed = addressed or {}

    async def get_phase(self, game_id: str) -> tuple[Phase, int] | None:
        return self._mapping.get(game_id)

    async def get_alive_seat_nos(self, game_id: str) -> list[int]:
        return self._alive.get(game_id, [])

    async def resolve_addressed_seat(
        self, game_id: str, addressed_name: str
    ) -> int | None:
        return self._addressed.get((game_id, addressed_name))


async def test_master_ingest_discards_npc_speaker(repo: SqliteRepo) -> None:
    registry = InMemoryNpcRegistry()
    registry.register(
        npc_id="npc1",
        discord_bot_user_id="bot1",
        supported_voices=(),
        version="1",
        send=None,
        now_ms=1, persona_key="setsu")
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    lookup = _StubPhaseLookup({"g1": (Phase.DAY_DISCUSSION, 1)})
    svc = MasterIngestService(registry=registry, discussion=discussion, phase_lookup=lookup)
    payload = SpeechEventPayload(
        ts=1,
        trace_id="t",
        game_id="g1",
        phase_id="p",
        seat_no=4,
        speaker_discord_user_id="bot1",
        segment_id="s1",
        text="勝手にtts",
        confidence=0.9,
        duration_ms=500,
        audio_start_ms=0,
        audio_end_ms=500,
    )
    event, reason = await svc.ingest_voice(payload)
    assert event is None
    assert reason == "npc_stt_discarded"
    rows = await store.load_for_game("g1")
    assert rows == []


async def test_master_ingest_accepts_human_speaker(repo: SqliteRepo) -> None:
    # Seed a real game so the foreign key on speech_events.game_id holds.
    g = Game(
        id="g1",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(g)
    await repo.insert_seat(
        "g1",
        Seat(seat_no=4, display_name="Hu", discord_user_id="u4", is_llm=False, persona_key=None),
    )
    await repo.set_player_role("g1", 4, Role.VILLAGER)
    registry = InMemoryNpcRegistry()
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    lookup = _StubPhaseLookup(
        {"g1": (Phase.DAY_DISCUSSION, 1)},
        alive_seats={"g1": [4]},
    )
    svc = MasterIngestService(registry=registry, discussion=discussion, phase_lookup=lookup)
    canonical_phase_id = make_phase_id("g1", 1, Phase.DAY_DISCUSSION)
    payload = SpeechEventPayload(
        ts=1,
        trace_id="t",
        game_id="g1",
        phase_id=canonical_phase_id,
        seat_no=4,
        speaker_discord_user_id="u4",
        segment_id="s1",
        text="人間のはずだよ",
        confidence=0.9,
        duration_ms=500,
        audio_start_ms=0,
        audio_end_ms=500,
    )
    event, reason = await svc.ingest_voice(payload)
    assert reason is None
    assert event is not None
    assert event.source == SpeechSource.VOICE_STT
    # The event must use the canonical phase_id computed on Master.
    assert event.phase_id == canonical_phase_id
    rows = await store.load_phase("g1", canonical_phase_id)
    assert any(r.text == "人間のはずだよ" for r in rows)
    # A phase_baseline sentinel must have been seeded.
    baselines = [r for r in rows if r.source == SpeechSource.PHASE_BASELINE]
    assert len(baselines) == 1


async def test_master_ingest_drops_dead_speaker_stt(repo: SqliteRepo) -> None:
    """Defensive alive check: a delayed STT result for a player who
    died mid-segment must not produce a SpeechEvent. The voice-ingest
    VAD entry already filters dead audio, but a slow Gemini response
    could land after death — Master is the last line of defense."""
    g = Game(
        id="g_dead",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(g)
    await repo.insert_seat(
        "g_dead",
        Seat(seat_no=4, display_name="Dead", discord_user_id="u4", is_llm=False, persona_key=None),
    )
    await repo.set_player_role("g_dead", 4, Role.VILLAGER)
    registry = InMemoryNpcRegistry()
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    # alive_seats does NOT include seat 4 — i.e. they're already dead.
    lookup = _StubPhaseLookup(
        {"g_dead": (Phase.DAY_DISCUSSION, 1)},
        alive_seats={"g_dead": []},
    )
    svc = MasterIngestService(
        registry=registry, discussion=discussion, phase_lookup=lookup
    )
    payload = SpeechEventPayload(
        ts=1,
        trace_id="t",
        game_id="g_dead",
        phase_id=make_phase_id("g_dead", 1, Phase.DAY_DISCUSSION),
        seat_no=4,
        speaker_discord_user_id="u4",
        segment_id="s_late",
        text="まだ生きてる…",
        confidence=0.9,
        duration_ms=500,
        audio_start_ms=0,
        audio_end_ms=500,
    )
    event, reason = await svc.ingest_voice(payload)
    assert event is None
    assert reason == "dead_speaker_discarded"
    # Critically: NO SpeechEvent row got persisted (not even baseline).
    rows = await store.load_for_game("g_dead")
    assert rows == []


async def test_master_ingest_ignores_caller_phase_id(repo: SqliteRepo) -> None:
    """MasterIngestService must compute the canonical phase_id on Master,
    not trust the caller-supplied value from the voice-ingest worker."""
    g = Game(
        id="gX",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=2,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(g)
    await repo.insert_seat(
        "gX",
        Seat(seat_no=3, display_name="Hu", discord_user_id="u3", is_llm=False, persona_key=None),
    )
    await repo.set_player_role("gX", 3, Role.VILLAGER)
    registry = InMemoryNpcRegistry()
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    lookup = _StubPhaseLookup(
        {"gX": (Phase.DAY_DISCUSSION, 2)},
        alive_seats={"gX": [3]},
    )
    svc = MasterIngestService(registry=registry, discussion=discussion, phase_lookup=lookup)

    # Caller supplies a stale phase_id from day 1 — Master must override.
    stale_phase_id = make_phase_id("gX", 1, Phase.DAY_DISCUSSION)
    canonical_phase_id = make_phase_id("gX", 2, Phase.DAY_DISCUSSION)
    payload = SpeechEventPayload(
        ts=1,
        trace_id="t",
        game_id="gX",
        phase_id=stale_phase_id,
        seat_no=3,
        speaker_discord_user_id="u3",
        segment_id="s2",
        text="テスト",
        confidence=0.9,
        duration_ms=300,
        audio_start_ms=0,
        audio_end_ms=300,
    )
    event, reason = await svc.ingest_voice(payload)
    assert reason is None
    assert event is not None
    assert event.phase_id == canonical_phase_id
    assert event.phase_id != stale_phase_id


# ---------------------------------------------------------------- audit tables


async def test_audit_tables_round_trip(repo: SqliteRepo) -> None:
    g = Game(
        id="ga",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(g)

    await repo.insert_npc_speak_request(
        request_id="r1",
        game_id="ga",
        phase_id="ph",
        npc_id="np",
        seat_no=2,
        logic_packet_id="lp",
        suggested_intent="counter",
        max_chars=80,
        max_duration_ms=8000,
        priority=1,
        expires_at_ms=10_000,
        created_at_ms=1000,
    )
    open_reqs = await repo.load_open_npc_speak_requests("ga")
    assert len(open_reqs) == 1 and open_reqs[0]["request_id"] == "r1"

    await repo.insert_npc_speak_result(
        request_id="r1",
        game_id="ga",
        phase_id="ph",
        npc_id="np",
        status="accepted",
        text="それは違うよ",
        used_logic_ids=["c1", "c2"],
        intent="counter",
        estimated_duration_ms=2500,
        failure_reason=None,
        received_at_ms=1100,
    )
    open_reqs = await repo.load_open_npc_speak_requests("ga")
    assert open_reqs == []  # closed by result

    await repo.open_npc_playback(
        request_id="r1",
        game_id="ga",
        phase_id="ph",
        npc_id="np",
        speech_event_id="ev1",
        authorized_at_ms=1200,
        playback_deadline_ms=11_000,
    )
    open_play = await repo.load_open_npc_playback("ga")
    assert len(open_play) == 1

    await repo.update_npc_playback_tts(
        "r1", outcome="success", duration_ms=500, failure_reason=None
    )
    await repo.close_npc_playback(
        "r1", finished_at_ms=2000, outcome="succeeded", failure_reason=None
    )
    open_play = await repo.load_open_npc_playback("ga")
    assert open_play == []


async def test_close_npc_playback_only_affects_open_rows(repo: SqliteRepo) -> None:
    """A second close_npc_playback after the row is already closed must not
    overwrite the original outcome."""
    g = Game(
        id="gb",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(g)
    await repo.open_npc_playback(
        request_id="r2",
        game_id="gb",
        phase_id="ph",
        npc_id="np",
        speech_event_id="ev1",
        authorized_at_ms=1,
        playback_deadline_ms=2,
    )
    await repo.close_npc_playback("r2", finished_at_ms=3, outcome="succeeded", failure_reason=None)
    # Second close is a no-op.
    await repo.close_npc_playback("r2", finished_at_ms=99, outcome="failed", failure_reason="oops")
    open_play = await repo.load_open_npc_playback("gb")
    assert open_play == []


# ---------------------------------------------------------------- protocol smoke


def test_phase_lookup_protocol_runtime_check() -> None:
    """A class with `get_phase`, `get_alive_seat_nos`, and
    `resolve_addressed_seat` satisfies PhaseLookup."""

    class Impl:
        async def get_phase(self, game_id: str) -> tuple[Phase, int] | None:
            return None

        async def get_alive_seat_nos(self, game_id: str) -> list[int]:
            return []

        async def resolve_addressed_seat(
            self, game_id: str, addressed_name: str
        ) -> int | None:
            return None

    obj = Impl()
    # Use the runtime-checkable protocol; this asserts the structural match.
    assert isinstance(obj, PhaseLookup)


@pytest.mark.parametrize("psk_match", [True, False])
def test_websockets_master_ws_server_constructs_without_starting(
    psk_match: bool,
) -> None:
    """Constructor wiring smoke — no actual socket bind."""
    from wolfbot.master.ws.ws_server import WebsocketsMasterWsServer

    registry = InMemoryNpcRegistry()
    handlers = MasterHandlers(registry=registry, now_ms=lambda: 0)
    server = WebsocketsMasterWsServer(
        host="127.0.0.1",
        port=8888,
        psk="secret" if psk_match else "other",
        registry=registry,
        handlers=handlers,
    )
    assert server.psk == ("secret" if psk_match else "other")
    assert server.host == "127.0.0.1"
