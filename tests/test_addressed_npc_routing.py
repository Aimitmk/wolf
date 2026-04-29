"""Addressed-NPC priority routing — end-to-end coverage.

Verifies the four-layer change so a human's named-address ('セツさん、どう
思う？') is dispatched to the addressed NPC seat first, with the utterance
text forwarded to that NPC's logic packet so the LLM can actually reply
on-topic:

1. Gemini analyzer prompt parses `addressed_name` from the JSON envelope.
2. `MasterIngestService.ingest_voice` resolves `addressed_name` against the
   live seats table to populate `SpeechEvent.addressed_seat_no`.
3. `PublicDiscussionState` fold tracks the most recent unanswered address.
4. `SpeakArbiter.try_dispatch_next` picks the addressed NPC ahead of silent
   seats and lowest-seat tiebreaker, and `build_logic_packet` includes the
   utterance in the packet summary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from wolfbot.domain.discussion import (
    PublicDiscussionState,
    make_phase_id,
)
from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Seat
from wolfbot.domain.ws_messages import SpeechEventPayload
from wolfbot.master.ingest_service import (
    MasterIngestService,
    resolve_seat_by_name,
)
from wolfbot.master.logic_service import build_logic_packet
from wolfbot.master.npc_registry import InMemoryNpcRegistry
from wolfbot.master.speak_arbiter import SpeakArbiter
from wolfbot.master.stt_service import GeminiAudioAnalyzer
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discussion_service import (
    DiscussionService,
    SqliteSpeechEventStore,
    make_human_text_event,
    make_npc_generated_event,
    make_phase_baseline,
    rebuild_public_state_from_events,
)

# ------------------------------------------------------ Layer 1: Gemini parser


def test_gemini_analyzer_parses_addressed_name() -> None:
    """The analyzer's JSON parser must surface `addressed_name` on SttResult."""
    raw = (
        '{"transcript":"セツさん、どう思う？","summary":"セツに意見を求めた",'
        '"confidence":0.91,"co_claim":null,"vote_target_seat":null,'
        '"stance":{},"addressed_name":"セツ"}'
    )
    parsed = GeminiAudioAnalyzer._parse_response(raw)
    assert parsed.get("addressed_name") == "セツ"


def test_gemini_analyzer_addressed_name_null_when_global_address() -> None:
    """Non-personal addresses ('みんな') must not surface as addressed_name."""
    raw = (
        '{"transcript":"みんなどう思う？","summary":"全体への問いかけ",'
        '"confidence":0.88,"co_claim":null,"vote_target_seat":null,'
        '"stance":{},"addressed_name":null}'
    )
    parsed = GeminiAudioAnalyzer._parse_response(raw)
    assert parsed.get("addressed_name") is None


# ------------------------------------------------------ Layer 2: name → seat resolver


def _seat(no: int, *, name: str, persona: str | None = None, llm: bool = True) -> Seat:
    return Seat(
        seat_no=no,
        display_name=name,
        discord_user_id=None if llm else f"u{no}",
        is_llm=llm,
        persona_key=persona,
    )


def test_resolve_seat_by_name_strips_emoji_and_honorific() -> None:
    seats = [
        _seat(1, name="Alice", persona=None, llm=False),
        _seat(2, name="🌙セツ", persona="setsu"),
        _seat(3, name="🟣ジナ", persona="gina"),
    ]
    assert resolve_seat_by_name("セツさん", seats) == 2
    assert resolve_seat_by_name("ジナ", seats) == 3
    assert resolve_seat_by_name("ジナちゃん", seats) == 3


def test_resolve_seat_by_name_recognises_seat_number_forms() -> None:
    seats = [
        _seat(1, name="Alice", persona=None, llm=False),
        _seat(2, name="🌙セツ", persona="setsu"),
        _seat(3, name="🟣ジナ", persona="gina"),
    ]
    assert resolve_seat_by_name("席3", seats) == 3
    assert resolve_seat_by_name("3番", seats) == 3
    assert resolve_seat_by_name("seat 2", seats) == 2


def test_resolve_seat_by_name_returns_none_for_unknown_or_dead() -> None:
    seats = [
        _seat(2, name="🌙セツ", persona="setsu"),
        _seat(3, name="🟣ジナ", persona="gina"),
    ]
    assert resolve_seat_by_name("ククルシカ", seats) is None
    # Dead seat — must not return it even if name matches.
    assert resolve_seat_by_name("セツ", seats, alive=frozenset({3})) is None


def test_resolve_seat_by_name_returns_none_on_ambiguity() -> None:
    seats = [
        _seat(2, name="Alice", persona=None, llm=False),
        _seat(3, name="Alice", persona=None, llm=False),
    ]
    assert resolve_seat_by_name("Alice", seats) is None


# ------------------------------------------------------ Layer 2: ingest service


class _SeatedPhaseLookup:
    def __init__(self, seats: list[Seat], alive: list[int]) -> None:
        self._seats = seats
        self._alive = alive

    async def get_phase(self, game_id: str) -> tuple[Phase, int] | None:
        return (Phase.DAY_DISCUSSION, 1)

    async def get_alive_seat_nos(self, game_id: str) -> list[int]:
        return list(self._alive)

    async def resolve_addressed_seat(
        self, game_id: str, addressed_name: str
    ) -> int | None:
        return resolve_seat_by_name(
            addressed_name, self._seats, alive=frozenset(self._alive)
        )


async def _make_seated_game(repo: SqliteRepo) -> tuple[Game, list[Seat]]:
    g = Game(
        id="g_addr",
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
        _seat(1, name="Alice", persona=None, llm=False),
        _seat(2, name="🌙セツ", persona="setsu"),
        _seat(3, name="🟣ジナ", persona="gina"),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 2, Role.SEER)
    await repo.set_player_role(g.id, 3, Role.VILLAGER)
    return g, seats


async def test_ingest_voice_sets_addressed_seat_from_payload(
    repo: SqliteRepo,
) -> None:
    g, seats = await _make_seated_game(repo)
    registry = InMemoryNpcRegistry()
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    lookup = _SeatedPhaseLookup(seats=seats, alive=[1, 2, 3])
    svc = MasterIngestService(
        registry=registry, discussion=discussion, phase_lookup=lookup
    )
    payload = SpeechEventPayload(
        ts=1,
        trace_id="t",
        game_id=g.id,
        phase_id=make_phase_id(g.id, 1, Phase.DAY_DISCUSSION),
        seat_no=1,
        speaker_discord_user_id="u1",
        segment_id="s1",
        text="セツさん、どう思う？",
        confidence=0.92,
        duration_ms=600,
        audio_start_ms=0,
        audio_end_ms=600,
        addressed_name="セツ",
    )
    event, reason = await svc.ingest_voice(payload)
    assert reason is None
    assert event is not None
    assert event.addressed_seat_no == 2
    rows = await store.load_phase(g.id, event.phase_id)
    assert any(r.event_id == event.event_id and r.addressed_seat_no == 2 for r in rows)


async def test_ingest_voice_prefers_payload_seat_no_over_name_resolution(
    repo: SqliteRepo,
) -> None:
    """When the analyzer's prompt was grounded with a roster, the
    ``addressed_seat_no`` it pre-resolves must override the legacy
    string-based ``resolve_seat_by_name`` lookup. This is the only
    code path that handles a renamed-bot scenario where the live VC
    nickname doesn't match the persona's stored ``Seat.display_name``.

    Test setup: seat 2 has display_name "🌙セツ" in the DB. The payload
    carries ``addressed_name="Lucky"`` (a name that would never resolve
    via the legacy path) but ``addressed_seat_no=2``. The resulting
    ``SpeechEvent.addressed_seat_no`` must be 2.
    """
    g, seats = await _make_seated_game(repo)
    registry = InMemoryNpcRegistry()
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    lookup = _SeatedPhaseLookup(seats=seats, alive=[1, 2, 3])
    svc = MasterIngestService(
        registry=registry, discussion=discussion, phase_lookup=lookup
    )
    payload = SpeechEventPayload(
        ts=1,
        trace_id="t",
        game_id=g.id,
        phase_id=make_phase_id(g.id, 1, Phase.DAY_DISCUSSION),
        seat_no=1,
        speaker_discord_user_id="u1",
        segment_id="s1",
        text="Luckyさん、どう思う？",
        confidence=0.92,
        duration_ms=600,
        audio_start_ms=0,
        audio_end_ms=600,
        addressed_name="Lucky",
        addressed_seat_no=2,
    )
    event, reason = await svc.ingest_voice(payload)
    assert reason is None
    assert event is not None
    assert event.addressed_seat_no == 2


async def test_ingest_voice_falls_back_to_name_when_seat_no_dead(
    repo: SqliteRepo,
) -> None:
    """A hallucinated / stale ``addressed_seat_no`` pointing at a dead
    seat must be rejected and the resolver must fall back to the
    name-based path."""
    g, seats = await _make_seated_game(repo)
    registry = InMemoryNpcRegistry()
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    # Seat 2 is dead in this scenario; analyzer hallucinates
    # ``addressed_seat_no=2`` anyway.
    lookup = _SeatedPhaseLookup(seats=seats, alive=[1, 3])
    svc = MasterIngestService(
        registry=registry, discussion=discussion, phase_lookup=lookup
    )
    payload = SpeechEventPayload(
        ts=1,
        trace_id="t",
        game_id=g.id,
        phase_id=make_phase_id(g.id, 1, Phase.DAY_DISCUSSION),
        seat_no=1,
        speaker_discord_user_id="u1",
        segment_id="s1",
        text="ジナさんどう？",
        confidence=0.92,
        duration_ms=600,
        audio_start_ms=0,
        audio_end_ms=600,
        addressed_name="ジナ",
        addressed_seat_no=2,  # dead — should be ignored
    )
    event, reason = await svc.ingest_voice(payload)
    assert reason is None
    assert event is not None
    # Falls back to name resolution → resolves "ジナ" to seat 3.
    assert event.addressed_seat_no == 3


async def test_ingest_voice_self_address_is_dropped(repo: SqliteRepo) -> None:
    """A speaker calling their own name must not produce a routed address."""
    g, seats = await _make_seated_game(repo)
    registry = InMemoryNpcRegistry()
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
    lookup = _SeatedPhaseLookup(seats=seats, alive=[1, 2, 3])
    svc = MasterIngestService(
        registry=registry, discussion=discussion, phase_lookup=lookup
    )
    payload = SpeechEventPayload(
        ts=1,
        trace_id="t",
        game_id=g.id,
        phase_id=make_phase_id(g.id, 1, Phase.DAY_DISCUSSION),
        seat_no=2,  # speaker is セツ
        speaker_discord_user_id="u2",
        segment_id="s1",
        text="セツです、よろしく",
        confidence=0.92,
        duration_ms=600,
        audio_start_ms=0,
        audio_end_ms=600,
        addressed_name="セツ",
    )
    event, reason = await svc.ingest_voice(payload)
    assert reason is None
    assert event is not None
    assert event.addressed_seat_no is None


# ------------------------------------------------------ Layer 3: state fold


def test_public_state_fold_tracks_last_addressed_seat() -> None:
    phase_id = make_phase_id("g", 1, Phase.DAY_DISCUSSION)
    sentinel = make_phase_baseline(
        game_id="g",
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=[1, 2, 3],
        created_at_ms=1,
    )
    addressed = make_human_text_event(
        game_id="g",
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=1,
        text="セツさん、どう思う？",
        created_at_ms=2,
    ).model_copy(update={"addressed_seat_no": 2})

    state = rebuild_public_state_from_events([sentinel, addressed])
    assert state is not None
    assert state.last_addressed_seat == 2
    assert state.last_addressed_speaker_seat == 1
    assert "セツさん" in state.last_addressed_text


def test_public_state_fold_clears_address_after_npc_reply() -> None:
    phase_id = make_phase_id("g", 1, Phase.DAY_DISCUSSION)
    sentinel = make_phase_baseline(
        game_id="g",
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=[1, 2, 3],
        created_at_ms=1,
    )
    addressed = make_human_text_event(
        game_id="g",
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=1,
        text="セツさん、どう思う？",
        created_at_ms=2,
    ).model_copy(update={"addressed_seat_no": 2})
    npc_reply = make_npc_generated_event(
        game_id="g",
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=2,
        text="そうだね",
        created_at_ms=3,
    )

    state = rebuild_public_state_from_events([sentinel, addressed, npc_reply])
    assert state is not None
    assert state.last_addressed_seat is None
    assert state.last_addressed_text == ""


# ------------------------------------------------------ Layer 4: arbiter picker


def _captured_send(buf: list[str]) -> Callable[[str], Awaitable[None]]:
    async def send(msg: str) -> None:
        buf.append(msg)

    return send


async def test_speak_arbiter_prefers_addressed_seat_over_silent_lowest(
    repo: SqliteRepo,
) -> None:
    """Even when seat 2 is also silent (and would otherwise win on the
    lowest-seat tiebreaker), the addressed seat 3 must be picked."""
    g, _seats = await _make_seated_game(repo)
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
    addressed = make_human_text_event(
        game_id=g.id,
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=1,
        text="ジナさん、どう思う？",
        created_at_ms=2,
    ).model_copy(update={"addressed_seat_no": 3})
    await store.insert(addressed)

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
        repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 1500
    )
    await arb.try_dispatch_next(g.id)

    assert buf3, "addressed NPC at seat 3 must be dispatched first"
    assert not buf2, "lowest-seat NPC at seat 2 must not preempt the addressed one"


async def test_speak_arbiter_skips_addressed_when_offline_falls_back_to_silent(
    repo: SqliteRepo,
) -> None:
    """Address resolution must not strand the discussion: if the named NPC
    is offline, the picker should fall through to the existing silent-first
    rotation rather than no-op."""
    g, _seats = await _make_seated_game(repo)
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
    # Address seat 3, but only seat 2's NPC is online.
    addressed = make_human_text_event(
        game_id=g.id,
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=1,
        text="ジナさん、どう？",
        created_at_ms=2,
    ).model_copy(update={"addressed_seat_no": 3})
    await store.insert(addressed)

    registry = InMemoryNpcRegistry()
    buf2: list[str] = []
    registry.register(
        npc_id="npc_setsu",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_captured_send(buf2),
        now_ms=1000,
        persona_key="setsu",
    )
    registry.assign("npc_setsu", seat=2, game_id=g.id, phase_id=phase_id)

    arb = SpeakArbiter(
        repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 1500
    )
    await arb.try_dispatch_next(g.id)

    assert buf2, "fallback to the only online silent NPC must still happen"


# ------------------------------------------------------ Layer 4: logic packet


def test_logic_packet_summary_includes_address_signal() -> None:
    state = PublicDiscussionState(
        game_id="g",
        phase_id="g::day1::DAY_DISCUSSION::1",
        day=1,
        alive_seat_nos=frozenset({1, 2, 3}),
        last_addressed_seat=2,
        last_addressed_speaker_seat=1,
        last_addressed_text="セツさん、占い結果はどうだった？",
    )
    packet = build_logic_packet(
        state=state,
        recipient_npc_id="npc_setsu",
        expires_at_ms=2000,
        now_ms=1500,
    )
    assert "last_address=席2" in packet.public_state_summary
    assert "from=席1" in packet.public_state_summary
    assert "セツさん" in packet.public_state_summary


def test_logic_packet_summary_omits_address_when_none() -> None:
    state = PublicDiscussionState(
        game_id="g",
        phase_id="g::day1::DAY_DISCUSSION::1",
        day=1,
        alive_seat_nos=frozenset({1, 2, 3}),
    )
    packet = build_logic_packet(
        state=state,
        recipient_npc_id="npc_setsu",
        expires_at_ms=2000,
        now_ms=1500,
    )
    assert "last_address" not in packet.public_state_summary


# ------------------------------------------------------ End-to-end: payload → arbiter


async def test_wolfcog_on_message_uses_text_analyzer_to_set_addressed_seat(
    repo: SqliteRepo,
) -> None:
    """When a TextAnalyzer is wired, `WolfCog.on_message` must call it and
    propagate the resolved `addressed_seat_no` onto the SpeechEvent so the
    text path matches the voice path."""
    from unittest.mock import MagicMock

    from wolfbot.domain.discussion import SpeechSource
    from wolfbot.master.text_analyzer import FakeTextAnalyzer, TextAnalysis
    from wolfbot.services.discord_service import WolfCog
    from wolfbot.services.discussion_service import SqliteSpeechEventStore

    g, _seats = await _make_seated_game(repo)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store, log_sink=repo)
    fake_analyzer = FakeTextAnalyzer(
        scripted=[TextAnalysis(addressed_name="ジナ")]
    )

    cog = WolfCog(
        bot=MagicMock(),
        repo=repo,
        game_service=MagicMock(),
        discord_adapter=MagicMock(),
        llm_adapter=MagicMock(),
        registry=MagicMock(),
        settings=MagicMock(MAIN_TEXT_CHANNEL_ID=100, MAIN_VOICE_CHANNEL_ID=200),
        discussion_service=discussion,
        text_analyzer=fake_analyzer,
    )

    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = "u1"
    msg.guild.id = g.guild_id
    msg.channel.id = "c1"  # main_text_channel_id
    msg.content = "ジナさん、どう思う？"

    # Patch the message channel to match game.main_text_channel_id ("c1").
    await WolfCog.on_message(cog, msg)

    # The fake analyzer should have been called once with the message body.
    assert fake_analyzer.call_count == 1
    assert fake_analyzer.last_text == "ジナさん、どう思う？"

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    events = await store.load_phase(g.id, phase_id)
    text_events = [e for e in events if e.source == SpeechSource.TEXT]
    assert len(text_events) == 1
    assert text_events[0].addressed_seat_no == 3
    assert text_events[0].speaker_seat == 1


async def test_wolfcog_on_message_skips_addressed_when_analyzer_fails(
    repo: SqliteRepo,
) -> None:
    """A broken analyzer must not block the SpeechEvent write — the row
    should still land with `addressed_seat_no=None` so plain text capture
    keeps working."""
    from unittest.mock import MagicMock

    from wolfbot.domain.discussion import SpeechSource
    from wolfbot.master.text_analyzer import FakeTextAnalyzer
    from wolfbot.services.discord_service import WolfCog
    from wolfbot.services.discussion_service import SqliteSpeechEventStore

    g, _seats = await _make_seated_game(repo)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store, log_sink=repo)
    broken = FakeTextAnalyzer(scripted=[RuntimeError("provider down")])

    cog = WolfCog(
        bot=MagicMock(),
        repo=repo,
        game_service=MagicMock(),
        discord_adapter=MagicMock(),
        llm_adapter=MagicMock(),
        registry=MagicMock(),
        settings=MagicMock(MAIN_TEXT_CHANNEL_ID=100, MAIN_VOICE_CHANNEL_ID=200),
        discussion_service=discussion,
        text_analyzer=broken,
    )

    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = "u1"
    msg.guild.id = g.guild_id
    msg.channel.id = "c1"
    msg.content = "セツさん、どう？"

    await WolfCog.on_message(cog, msg)

    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    events = await store.load_phase(g.id, phase_id)
    text_events = [e for e in events if e.source == SpeechSource.TEXT]
    assert len(text_events) == 1
    assert text_events[0].addressed_seat_no is None
    assert text_events[0].text == "セツさん、どう？"


async def test_end_to_end_addressed_dispatch(repo: SqliteRepo) -> None:
    """A SpeechEventPayload carrying `addressed_name='ジナ'` must result in
    the seat-3 NPC receiving the next SpeakRequest, not seat 2."""
    g, seats = await _make_seated_game(repo)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)
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
    phase_id = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    registry.assign("npc_setsu", seat=2, game_id=g.id, phase_id=phase_id)
    registry.assign("npc_gina", seat=3, game_id=g.id, phase_id=phase_id)

    lookup = _SeatedPhaseLookup(seats=seats, alive=[1, 2, 3])
    ingest = MasterIngestService(
        registry=registry, discussion=discussion, phase_lookup=lookup
    )
    arb = SpeakArbiter(
        repo=repo, registry=registry, discussion=discussion, now_ms=lambda: 1500
    )

    payload = SpeechEventPayload(
        ts=1,
        trace_id="t",
        game_id=g.id,
        phase_id=phase_id,
        seat_no=1,
        speaker_discord_user_id="u1",
        segment_id="s1",
        text="ジナさん、どう思う？",
        confidence=0.92,
        duration_ms=600,
        audio_start_ms=0,
        audio_end_ms=600,
        addressed_name="ジナ",
    )
    event, reason = await ingest.ingest_voice(payload)
    assert reason is None and event is not None and event.addressed_seat_no == 3

    await arb.try_dispatch_next(g.id)
    assert buf3, "addressed NPC at seat 3 must be dispatched"
    assert not buf2, "non-addressed NPC at seat 2 must wait"
    # The dispatched logic packet must carry the human's utterance so the
    # NPC's LLM has actual context to reply to.
    assert any("ジナさん" in m for m in buf3)


# -- Layer 5: NPC's own addressed_seat_no propagates through state ----


def test_npc_speech_with_addressed_seat_sets_last_addressed() -> None:
    """When an NPC's structured output sets `addressed_seat_no`, the
    PublicDiscussionState fold must surface it as `last_addressed_seat`
    so the next arbiter dispatch prioritizes the addressee. Reproduces
    the production bug where Raqio (seat 1) said "席9 ユリコ…" 8 times
    in a row because every NPC speech cleared the address pointer.
    """
    phase_id = make_phase_id("g", 1, Phase.DAY_DISCUSSION)
    events = [
        make_phase_baseline(
            game_id="g", phase_id=phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 9], created_at_ms=1,
        ),
        # NPC at seat 1 names seat 9 — this used to clear last_addressed.
        make_npc_generated_event(
            game_id="g", phase_id=phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=1, text="席9ユリコ、君が処刑候補だ",
            addressed_seat_no=9, created_at_ms=10,
        ),
    ]
    state = rebuild_public_state_from_events(events)
    assert state is not None
    assert state.last_addressed_seat == 9, (
        "NPC's addressed_seat_no must propagate to last_addressed_seat"
    )
    assert state.last_addressed_speaker_seat == 1


def test_unrelated_npc_speech_does_not_clear_pending_address() -> None:
    """If NPC A addresses seat X and a different NPC B jumps in (without
    naming anyone), the pending address must NOT be cleared — otherwise
    the arbiter loses the cue to prioritize X next. Pre-fix, every NPC
    speech wiped last_addressed_seat unconditionally.
    """
    phase_id = make_phase_id("g", 1, Phase.DAY_DISCUSSION)
    events = [
        make_phase_baseline(
            game_id="g", phase_id=phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 2, 9], created_at_ms=1,
        ),
        make_npc_generated_event(
            game_id="g", phase_id=phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=1, text="席9ユリコ、説明しろ",
            addressed_seat_no=9, created_at_ms=10,
        ),
        # Seat 2 jumps in without naming anyone — must not consume the
        # address pending for seat 9.
        make_npc_generated_event(
            game_id="g", phase_id=phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=2, text="一旦落ち着こう",
            addressed_seat_no=None, created_at_ms=20,
        ),
    ]
    state = rebuild_public_state_from_events(events)
    assert state is not None
    assert state.last_addressed_seat == 9, (
        "Unrelated NPC speech must not clear the pending address"
    )


async def test_co_claims_carry_across_phase_boundaries(repo: SqliteRepo) -> None:
    """Day-1 seer CO must still be visible in day-2's PublicDiscussionState
    so a wolf NPC can decide to counter-CO on day 2. Previously the fold
    rebuilt per-phase and the day-2 prompt showed `co_claims=[(none)]`
    even though day 1 had a clear seer CO.
    """
    g = Game(
        id="rv-co-carry",
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
        Seat(seat_no=1, display_name="Alice", discord_user_id="u1",
             is_llm=False, persona_key=None),
        Seat(seat_no=4, display_name="🦋ラキオ", discord_user_id=None,
             is_llm=True, persona_key="raqio"),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    await repo.set_player_role(g.id, 1, Role.WEREWOLF)
    await repo.set_player_role(g.id, 4, Role.SEER)

    day1_phase = make_phase_id(g.id, 1, Phase.DAY_DISCUSSION)
    day2_phase = make_phase_id(g.id, 2, Phase.DAY_DISCUSSION)
    store = SqliteSpeechEventStore(repo._conn)  # type: ignore[attr-defined]
    discussion = DiscussionService(store=store)

    # Day-1: Raqio CO's as seer.
    await store.insert(
        make_phase_baseline(
            game_id=g.id, phase_id=day1_phase, day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 4], created_at_ms=1,
        )
    )
    await store.insert(
        make_npc_generated_event(
            game_id=g.id, phase_id=day1_phase, day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=4, text="実は僕、占い師だ。",
            co_declaration="seer",
            created_at_ms=10,
        )
    )
    # Day-2: only baseline + a non-CO speech (no fresh CO this phase).
    await store.insert(
        make_phase_baseline(
            game_id=g.id, phase_id=day2_phase, day=2,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 4], created_at_ms=100,
        )
    )
    await store.insert(
        make_npc_generated_event(
            game_id=g.id, phase_id=day2_phase, day=2,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=1, text="昨日のことだけど",
            created_at_ms=110,
        )
    )

    arb = SpeakArbiter(
        repo=repo,
        registry=InMemoryNpcRegistry(),
        discussion=discussion,
        now_ms=lambda: 200,
    )
    state = await arb.rebuild_public_state(
        game_id=g.id, day=2, phase=Phase.DAY_DISCUSSION,
    )
    assert state is not None
    assert any(
        c.seat == 4 and c.role_claim == "seer" for c in state.co_claims
    ), f"day-2 state must carry day-1 seer CO, got co_claims={state.co_claims}"


def test_addressed_npc_reply_consumes_the_address() -> None:
    """When the addressed NPC actually replies (speaker_seat == prior
    last_addressed_seat) without naming a new target, the address is
    consumed so the arbiter doesn't keep prioritizing them forever."""
    phase_id = make_phase_id("g", 1, Phase.DAY_DISCUSSION)
    events = [
        make_phase_baseline(
            game_id="g", phase_id=phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
            alive_seat_nos=[1, 9], created_at_ms=1,
        ),
        make_npc_generated_event(
            game_id="g", phase_id=phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=1, text="席9ユリコ、答えろ",
            addressed_seat_no=9, created_at_ms=10,
        ),
        # Yuriko (seat 9) replies — this consumes the address.
        make_npc_generated_event(
            game_id="g", phase_id=phase_id, day=1,
            phase=Phase.DAY_DISCUSSION,
            speaker_seat=9, text="言いがかりだ",
            addressed_seat_no=None, created_at_ms=20,
        ),
    ]
    state = rebuild_public_state_from_events(events)
    assert state is not None
    assert state.last_addressed_seat is None
    assert state.last_addressed_speaker_seat is None


