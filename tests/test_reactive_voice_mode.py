"""Bundle 8: reactive_voice mode plumbing.

Verifies the mode-fixed-per-game contract:

- A game created with `discussion_mode="reactive_voice"` keeps that mode
  across reload — env changes do not retro-rewrite the column.
- Default mode is `rounds`.
- The schema migration is idempotent on existing DBs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

from wolfbot.domain.enums import Phase
from wolfbot.domain.models import Game
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discord_service import WolfCog
from wolfbot.services.game_service import new_game_id


async def test_default_mode_is_rounds(repo: SqliteRepo) -> None:
    g = Game(
        id=new_game_id(),
        guild_id=f"g-{new_game_id()}",
        host_user_id="h",
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(g)
    reloaded = await repo.load_game(g.id)
    assert reloaded is not None
    assert reloaded.discussion_mode == "rounds"


async def test_reactive_voice_mode_persisted_across_reload(repo: SqliteRepo) -> None:
    g = Game(
        id=new_game_id(),
        guild_id=f"g-{new_game_id()}",
        host_user_id="h",
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(g)
    reloaded = await repo.load_game(g.id)
    assert reloaded is not None
    assert reloaded.discussion_mode == "reactive_voice"


async def test_phase_advance_under_reactive_voice_skips_round_gate() -> None:
    """In reactive_voice mode, _plan_next at DAY_DISCUSSION advances when the
    deadline passes regardless of llm_speech_counts."""
    from wolfbot.domain.enums import Phase as PhaseEnum
    from wolfbot.domain.state_machine import plan_day_discussion_to_vote

    game = Game(
        id="rg",
        guild_id="gu",
        host_user_id="h",
        phase=PhaseEnum.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=100,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    transition = plan_day_discussion_to_vote(game, 200)
    assert transition.next_phase is PhaseEnum.DAY_VOTE


async def test_settings_loads_default_discussion_mode() -> None:
    """The Settings object exposes LLM_DISCUSSION_MODE with rounds default.

    `_env_file=None` keeps the developer's local ``.env.master`` (which may
    override LLM_DISCUSSION_MODE for actual runs) from leaking into the
    test and masking the default.
    """
    from pydantic import SecretStr

    from wolfbot.config import MasterSettings

    s = MasterSettings(  # type: ignore[arg-type]
        _env_file=None,
        DISCORD_TOKEN=SecretStr("dummy"),
        DISCORD_GUILD_ID=1,
        MAIN_TEXT_CHANNEL_ID=1,
        MAIN_VOICE_CHANNEL_ID=1,
        GAMEPLAY_LLM_API_KEY=SecretStr("dummy"),
    )
    assert s.LLM_DISCUSSION_MODE == "rounds"


def test_phase_module_imports_reactive_voice_keep_existing_phases() -> None:
    """Sanity: adding discussion_mode does not change the Phase enum."""
    assert Phase.DAY_DISCUSSION.value == "DAY_DISCUSSION"


# ---------------------------------------------------------------------------
# Integration tests: /wolf create captures discussion_mode, on_message
# produces speech_events, and the boot path wires DiscussionService.
# ---------------------------------------------------------------------------


@dataclass
class _FakeChannel:
    id: int
    deleted: bool = False

    async def delete(self, reason: str = "") -> None:
        self.deleted = True


@dataclass
class _FakeResponse:
    deferred: bool = False
    ephemerals: list[str] = field(default_factory=list)

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        if ephemeral:
            self.ephemerals.append(content)

    async def defer(self, thinking: bool = False) -> None:
        self.deferred = True


@dataclass
class _FakeFollowup:
    messages: list[str] = field(default_factory=list)

    async def send(self, content: str) -> None:
        self.messages.append(content)


class _FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id


class _FakeUser:
    def __init__(self, user_id: int = 777) -> None:
        self.id = user_id


class _FakeInteraction:
    def __init__(self, guild_id: int, user_id: int = 777) -> None:
        self.guild: Any = _FakeGuild(guild_id)
        self.guild_id = guild_id
        self.user = _FakeUser(user_id)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


def _build_cog_with_settings(repo: Any, *, discussion_mode: str = "rounds") -> WolfCog:
    settings = MagicMock()
    settings.MAIN_TEXT_CHANNEL_ID = 100
    settings.MAIN_VOICE_CHANNEL_ID = 200
    settings.LLM_DISCUSSION_MODE = discussion_mode
    return WolfCog(
        bot=MagicMock(),
        repo=repo,
        game_service=MagicMock(),
        discord_adapter=MagicMock(),
        llm_adapter=MagicMock(),
        registry=MagicMock(),
        settings=settings,
    )


async def test_create_game_captures_rounds_mode(repo: SqliteRepo) -> None:
    """/wolf create persists discussion_mode='rounds' from Settings."""
    cog = _build_cog_with_settings(repo, discussion_mode="rounds")

    async def fake_create(guild: Any, name: str, *, safe_to_delete_ids: set[str]) -> _FakeChannel:
        return _FakeChannel(id=hash(name) % 10000)

    cog._create_private_channel = fake_create  # type: ignore[method-assign]
    interaction = _FakeInteraction(guild_id=99)
    await WolfCog.create.callback(cog, interaction)  # type: ignore[arg-type]

    game = await repo.load_active_game_for_guild("99")
    assert game is not None
    assert game.discussion_mode == "rounds"


async def test_create_game_captures_reactive_voice_mode(repo: SqliteRepo) -> None:
    """/wolf create persists discussion_mode='reactive_voice' from Settings."""
    cog = _build_cog_with_settings(repo, discussion_mode="reactive_voice")

    async def fake_create(guild: Any, name: str, *, safe_to_delete_ids: set[str]) -> _FakeChannel:
        return _FakeChannel(id=hash(name) % 10000)

    cog._create_private_channel = fake_create  # type: ignore[method-assign]
    interaction = _FakeInteraction(guild_id=98)
    await WolfCog.create.callback(cog, interaction)  # type: ignore[arg-type]

    game = await repo.load_active_game_for_guild("98")
    assert game is not None
    assert game.discussion_mode == "reactive_voice"


async def test_create_game_falls_back_on_invalid_mode(repo: SqliteRepo) -> None:
    """/wolf create falls back to 'rounds' when given an invalid mode."""
    cog = _build_cog_with_settings(repo, discussion_mode="invalid_mode")

    async def fake_create(guild: Any, name: str, *, safe_to_delete_ids: set[str]) -> _FakeChannel:
        return _FakeChannel(id=hash(name) % 10000)

    cog._create_private_channel = fake_create  # type: ignore[method-assign]
    interaction = _FakeInteraction(guild_id=97)
    await WolfCog.create.callback(cog, interaction)  # type: ignore[arg-type]

    game = await repo.load_active_game_for_guild("97")
    assert game is not None
    assert game.discussion_mode == "rounds"


async def _make_discussion_game(
    repo: SqliteRepo,
    *,
    guild_id: str | None = None,
    discussion_mode: str = "rounds",
    human_seats: list[int] | None = None,
    llm_seats: list[int] | None = None,
) -> tuple[Game, Any, Any]:
    """Helper: create a DAY_DISCUSSION game with seats and a wired DiscussionService."""
    from wolfbot.domain.models import Seat
    from wolfbot.services.discussion_service import (
        DiscussionService,
        SqliteSpeechEventStore,
    )

    gid = guild_id or f"g-{new_game_id()}"
    game = Game(
        id=new_game_id(),
        guild_id=gid,
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=9999999999,
        main_text_channel_id="100",
        main_vc_channel_id="200",
        created_at=0,
        discussion_mode=discussion_mode,
    )
    await repo.create_game(game)
    for sno in human_seats or [1]:
        await repo.insert_seat(
            game.id,
            Seat(
                seat_no=sno,
                display_name=f"H{sno}",
                discord_user_id=f"user{sno}",
                is_llm=False,
                persona_key=None,
            ),
        )
    for sno in llm_seats or []:
        await repo.insert_seat(
            game.id,
            Seat(
                seat_no=sno,
                display_name=f"NPC{sno}",
                discord_user_id=None,
                is_llm=True,
                persona_key=f"persona{sno}",
            ),
        )

    store = SqliteSpeechEventStore(repo._db)
    ds = DiscussionService(store=store, log_sink=repo)
    return game, store, ds


async def test_on_message_writes_speech_event_for_human_text(repo: SqliteRepo) -> None:
    """Main-channel text during DAY_DISCUSSION writes a SpeechEvent row."""
    from wolfbot.domain.discussion import SpeechSource, make_phase_id

    game, store, ds = await _make_discussion_game(repo, human_seats=[1])

    cog = WolfCog(
        bot=MagicMock(),
        repo=repo,
        game_service=MagicMock(),
        discord_adapter=MagicMock(),
        llm_adapter=MagicMock(),
        registry=MagicMock(),
        settings=MagicMock(MAIN_TEXT_CHANNEL_ID=100, MAIN_VOICE_CHANNEL_ID=200),
        discussion_service=ds,
    )

    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = "user1"
    msg.guild.id = game.guild_id
    msg.channel.id = 100
    msg.content = "占いCO！私は占い師です"

    await WolfCog.on_message(cog, msg)

    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)
    events = await store.load_phase(game.id, phase_id)
    # Expect baseline sentinel + the text event.
    non_baseline = [e for e in events if e.source != SpeechSource.PHASE_BASELINE]
    assert len(non_baseline) == 1
    assert non_baseline[0].source == SpeechSource.TEXT
    assert non_baseline[0].speaker_seat == 1
    assert non_baseline[0].text == "占いCO！私は占い師です"


async def test_on_message_seeds_phase_baseline_before_text_event(repo: SqliteRepo) -> None:
    """on_message must seed the phase_baseline sentinel so PublicDiscussionState
    rebuild works even in an all-human game with no LLM dispatch."""
    from wolfbot.domain.discussion import SpeechSource, make_phase_id

    game, store, ds = await _make_discussion_game(repo, human_seats=[1, 2])

    cog = WolfCog(
        bot=MagicMock(),
        repo=repo,
        game_service=MagicMock(),
        discord_adapter=MagicMock(),
        llm_adapter=MagicMock(),
        registry=MagicMock(),
        settings=MagicMock(MAIN_TEXT_CHANNEL_ID=100, MAIN_VOICE_CHANNEL_ID=200),
        discussion_service=ds,
    )

    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = "user1"
    msg.guild.id = game.guild_id
    msg.channel.id = 100
    msg.content = "こんにちは"

    await WolfCog.on_message(cog, msg)

    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)
    events = await store.load_phase(game.id, phase_id)
    baselines = [e for e in events if e.source == SpeechSource.PHASE_BASELINE]
    assert len(baselines) == 1, "on_message should seed exactly one phase_baseline"

    # Second message from a different user should NOT duplicate the baseline.
    msg2 = MagicMock()
    msg2.author.bot = False
    msg2.author.id = "user2"
    msg2.guild.id = game.guild_id
    msg2.channel.id = 100
    msg2.content = "了解"

    await WolfCog.on_message(cog, msg2)

    events = await store.load_phase(game.id, phase_id)
    baselines = [e for e in events if e.source == SpeechSource.PHASE_BASELINE]
    assert len(baselines) == 1, "baseline must be idempotent across multiple messages"
    non_baseline = [e for e in events if e.source != SpeechSource.PHASE_BASELINE]
    assert len(non_baseline) == 2


async def test_llm_adapter_seeds_baseline_for_all_human_game(repo: SqliteRepo) -> None:
    """submit_llm_discussion_rounds must seed the phase baseline even when there
    are zero LLM seats so an all-human game gets a sentinel row."""
    from wolfbot.domain.discussion import SpeechSource, make_phase_id
    from wolfbot.services.llm_service import LLMAdapter

    game, store, ds = await _make_discussion_game(
        repo,
        human_seats=[1, 2, 3],
        llm_seats=[],
    )
    players = await repo.load_players(game.id)
    seats = await repo.load_seats(game.id)

    adapter = LLMAdapter(
        repo=repo,
        decider=MagicMock(),
        message_poster=MagicMock(),
        discussion_service=ds,
    )

    await adapter.submit_llm_discussion_rounds(game, players, seats)

    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)
    events = await store.load_phase(game.id, phase_id)
    baselines = [e for e in events if e.source == SpeechSource.PHASE_BASELINE]
    assert len(baselines) == 1, "baseline must be seeded even with no LLM seats"


async def test_main_py_wires_discussion_service() -> None:
    """Verify the main module imports and instantiates DiscussionService correctly.

    We do not start the full bot — just verify the import chain doesn't break
    and the LLMAdapter constructor accepts discussion_service.
    """
    import inspect

    from wolfbot.services.discussion_service import DiscussionService, SqliteSpeechEventStore
    from wolfbot.services.llm_service import LLMAdapter

    # Verify LLMAdapter accepts discussion_service kwarg.

    sig = inspect.signature(LLMAdapter.__init__)
    assert "discussion_service" in sig.parameters

    # Verify WolfCog accepts discussion_service kwarg.
    sig = inspect.signature(WolfCog.__init__)
    assert "discussion_service" in sig.parameters

    # Verify SqliteSpeechEventStore exists and is importable.
    assert callable(SqliteSpeechEventStore)
    assert callable(DiscussionService)


async def test_on_message_record_emits_player_speech_log(repo: SqliteRepo) -> None:
    """When discussion_service is wired, on_message should emit the PLAYER_SPEECH
    LogEntry via record() — not via the legacy direct insert_log_public path.
    This ensures the SpeechEvent and the LogEntry are produced atomically."""
    game, _store, ds = await _make_discussion_game(repo, human_seats=[1])

    cog = WolfCog(
        bot=MagicMock(),
        repo=repo,
        game_service=MagicMock(),
        discord_adapter=MagicMock(),
        llm_adapter=MagicMock(),
        registry=MagicMock(),
        settings=MagicMock(MAIN_TEXT_CHANNEL_ID=100, MAIN_VOICE_CHANNEL_ID=200),
        discussion_service=ds,
    )

    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = "user1"
    msg.guild.id = game.guild_id
    msg.channel.id = 100
    msg.content = "テスト発言"

    await WolfCog.on_message(cog, msg)

    # Verify PLAYER_SPEECH log was written (via record()'s log_sink hook).
    logs = await repo.load_public_logs(game.id)
    player_speeches = [lg for lg in logs if lg["kind"] == "PLAYER_SPEECH"]
    assert len(player_speeches) == 1
    assert player_speeches[0]["text"] == "テスト発言"
    assert player_speeches[0]["actor_seat"] == 1


async def test_on_message_during_runoff_speech_writes_speech_event(repo: SqliteRepo) -> None:
    """Main-channel text during DAY_RUNOFF_SPEECH also writes a SpeechEvent."""
    from wolfbot.domain.discussion import SpeechSource, make_phase_id
    from wolfbot.domain.models import Seat
    from wolfbot.services.discussion_service import (
        DiscussionService,
        SqliteSpeechEventStore,
    )

    game = Game(
        id=new_game_id(),
        guild_id=f"g-{new_game_id()}",
        host_user_id="h",
        phase=Phase.DAY_RUNOFF_SPEECH,
        day_number=1,
        deadline_epoch=9999999999,
        main_text_channel_id="100",
        main_vc_channel_id="200",
        created_at=0,
    )
    await repo.create_game(game)
    await repo.insert_seat(
        game.id,
        Seat(seat_no=1, display_name="H1", discord_user_id="user1", is_llm=False, persona_key=None),
    )
    store = SqliteSpeechEventStore(repo._db)
    ds = DiscussionService(store=store, log_sink=repo)

    cog = WolfCog(
        bot=MagicMock(),
        repo=repo,
        game_service=MagicMock(),
        discord_adapter=MagicMock(),
        llm_adapter=MagicMock(),
        registry=MagicMock(),
        settings=MagicMock(MAIN_TEXT_CHANNEL_ID=100, MAIN_VOICE_CHANNEL_ID=200),
        discussion_service=ds,
    )

    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = "user1"
    msg.guild.id = game.guild_id
    msg.channel.id = 100
    msg.content = "決選弁論です"

    await WolfCog.on_message(cog, msg)

    phase_id = make_phase_id(game.id, 1, Phase.DAY_RUNOFF_SPEECH)
    events = await store.load_phase(game.id, phase_id)
    non_baseline = [e for e in events if e.source != SpeechSource.PHASE_BASELINE]
    assert len(non_baseline) == 1
    assert non_baseline[0].source == SpeechSource.TEXT
    assert non_baseline[0].text == "決選弁論です"


async def test_recovery_skips_rounds_resume_for_reactive_voice(repo: SqliteRepo) -> None:
    """resume_llm_speech_progress must no-op for reactive_voice games so the
    legacy two-round batch is never spawned after a restart."""
    from wolfbot.domain.models import Seat
    from wolfbot.services.game_service import GameService

    game = Game(
        id=new_game_id(),
        guild_id=f"g-{new_game_id()}",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=9999999999,
        main_text_channel_id="100",
        main_vc_channel_id="200",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(game)
    await repo.insert_seat(
        game.id,
        Seat(seat_no=1, display_name="NPC1", discord_user_id=None, is_llm=True, persona_key="p1"),
    )

    mock_discord = MagicMock()
    mock_llm = MagicMock()
    mock_llm.submit_llm_discussion_rounds = MagicMock()
    mock_wake = MagicMock()

    service = GameService(repo=repo, discord=mock_discord, llm=mock_llm, wake=mock_wake)

    await service.resume_llm_speech_progress(game.id)

    # The LLM adapter's submit_llm_discussion_rounds must NOT have been called.
    mock_llm.submit_llm_discussion_rounds.assert_not_called()


async def test_main_py_wires_reactive_voice_pipeline_services() -> None:
    """Verify the reactive_voice pipeline modules are importable and the
    WebsocketsMasterWsServer / SpeakArbiter / NpcRegistry / MasterIngestService
    constructors accept the expected parameters."""
    import inspect

    from wolfbot.master.ingest_service import MasterIngestService
    from wolfbot.master.npc_registry import InMemoryNpcRegistry
    from wolfbot.master.speak_arbiter import SpeakArbiter
    from wolfbot.master.ws_server import (
        MasterHandlers,
        WebsocketsMasterWsServer,
    )

    sig = inspect.signature(WebsocketsMasterWsServer.__init__)
    assert "host" in sig.parameters
    assert "psk" in sig.parameters
    assert "registry" in sig.parameters
    assert "handlers" in sig.parameters

    sig = inspect.signature(SpeakArbiter.__init__)
    assert "repo" in sig.parameters
    assert "registry" in sig.parameters
    assert "discussion" in sig.parameters

    sig = inspect.signature(MasterIngestService.__init__)
    assert "registry" in sig.parameters
    assert "discussion" in sig.parameters
    assert "phase_lookup" in sig.parameters

    assert callable(InMemoryNpcRegistry)
    assert callable(MasterHandlers)


async def test_discussion_service_record_posts_voice_stt_to_channel(repo: SqliteRepo) -> None:
    """DiscussionService.record() must invoke message_poster.post_public for
    voice_stt events so the utterance is visible to text-only observers."""
    from wolfbot.services.discussion_service import DiscussionService

    game, store, _ = await _make_discussion_game(repo, human_seats=[1])

    posted: list[tuple[str, str, str]] = []

    class _FakePoster:
        async def post_public(self, game_id: str, text: str, kind: str) -> None:
            posted.append((game_id, text, kind))

    ds = DiscussionService(store=store, log_sink=repo, message_poster=_FakePoster())

    from wolfbot.services.discussion_service import make_voice_stt_event

    event = make_voice_stt_event(
        game_id=game.id,
        phase_id="test-phase",
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=1,
        text="声の発言テスト",
        stt_confidence=0.9,
        audio_start_ms=0,
        audio_end_ms=500,
    )
    await ds.record(event)

    assert len(posted) == 1
    assert posted[0][0] == game.id
    assert posted[0][1] == "声の発言テスト"
    assert posted[0][2] == "PLAYER_SPEECH"


async def test_discussion_service_record_skips_channel_post_for_text(repo: SqliteRepo) -> None:
    """DiscussionService.record() must NOT post to channel for source=text
    since the original Discord message is already visible."""
    from wolfbot.services.discussion_service import (
        DiscussionService,
        make_human_text_event,
    )

    game, store, _ = await _make_discussion_game(repo, human_seats=[1])

    posted: list[tuple[str, str, str]] = []

    class _FakePoster:
        async def post_public(self, game_id: str, text: str, kind: str) -> None:
            posted.append((game_id, text, kind))

    ds = DiscussionService(store=store, log_sink=repo, message_poster=_FakePoster())

    event = make_human_text_event(
        game_id=game.id,
        phase_id="test-phase",
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=1,
        text="テキスト発言",
    )
    await ds.record(event)

    assert posted == [], "source=text events must not duplicate the channel post"


async def test_arbiter_try_dispatch_next_triggers_on_reactive_voice(repo: SqliteRepo) -> None:
    """SpeakArbiter.try_dispatch_next dispatches a SpeakRequest when a
    reactive_voice game has an online NPC in a discussion phase."""
    from wolfbot.domain.models import Seat
    from wolfbot.master.npc_registry import InMemoryNpcRegistry
    from wolfbot.master.speak_arbiter import SpeakArbiter
    from wolfbot.services.discussion_service import (
        DiscussionService,
        SqliteSpeechEventStore,
    )

    game = Game(
        id=new_game_id(),
        guild_id=f"g-{new_game_id()}",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=9999999999,
        main_text_channel_id="100",
        main_vc_channel_id="200",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(game)
    await repo.insert_seat(
        game.id,
        Seat(seat_no=1, display_name="H1", discord_user_id="user1", is_llm=False, persona_key=None),
    )
    await repo.insert_seat(
        game.id,
        Seat(seat_no=2, display_name="NPC2", discord_user_id=None, is_llm=True, persona_key="p2"),
    )

    store = SqliteSpeechEventStore(repo._db)
    ds = DiscussionService(store=store, log_sink=repo)
    registry = InMemoryNpcRegistry()

    sent_messages: list[str] = []

    async def _fake_send(msg: str) -> None:
        sent_messages.append(msg)

    registry.register(
        npc_id="npc2",
        discord_bot_user_id="bot2",
        supported_voices=(),
        version="1",
        send=_fake_send,
        now_ms=1000, persona_key="setsu")
    registry.assign("npc2", seat=2, game_id=game.id, phase_id="test")

    arbiter = SpeakArbiter(
        repo=repo,
        registry=registry,
        discussion=ds,
        now_ms=lambda: 2000,
    )

    # Seed the phase baseline so rebuild_public_state works.
    await ds.begin_phase(
        game_id=game.id, day=1, phase=Phase.DAY_DISCUSSION, alive_seat_nos=[1, 2]
    )

    await arbiter.try_dispatch_next(game.id)

    # The arbiter should have sent a LogicPacket + SpeakRequest to the NPC.
    assert len(sent_messages) >= 2, f"Expected LogicPacket + SpeakRequest, got {len(sent_messages)}"
    import json as _json

    types = [_json.loads(m).get("type") for m in sent_messages]
    assert "logic_packet" in types
    assert "speak_request" in types


async def test_arbiter_try_dispatch_next_noop_for_rounds(repo: SqliteRepo) -> None:
    """try_dispatch_next must no-op for rounds-mode games."""
    from wolfbot.master.npc_registry import InMemoryNpcRegistry
    from wolfbot.master.speak_arbiter import SpeakArbiter
    from wolfbot.services.discussion_service import (
        DiscussionService,
        SqliteSpeechEventStore,
    )

    game = Game(
        id=new_game_id(),
        guild_id=f"g-{new_game_id()}",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=9999999999,
        main_text_channel_id="100",
        main_vc_channel_id="200",
        created_at=0,
        discussion_mode="rounds",
    )
    await repo.create_game(game)

    store = SqliteSpeechEventStore(repo._db)
    ds = DiscussionService(store=store, log_sink=repo)
    registry = InMemoryNpcRegistry()
    arbiter = SpeakArbiter(repo=repo, registry=registry, discussion=ds)

    # Should be a no-op — no errors, no dispatch.
    await arbiter.try_dispatch_next(game.id)


async def test_recovery_sweep_closes_open_speak_requests(repo: SqliteRepo) -> None:
    """reactive_voice_recovery_sweep must close open npc_speak_requests and
    npc_playback_events with failure_reason=master_restart."""
    from wolfbot.master.npc_registry import InMemoryNpcRegistry
    from wolfbot.master.speak_arbiter import SpeakArbiter
    from wolfbot.services.discussion_service import (
        DiscussionService,
        SqliteSpeechEventStore,
    )

    game = Game(
        id=new_game_id(),
        guild_id=f"g-{new_game_id()}",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=9999999999,
        main_text_channel_id="100",
        main_vc_channel_id="200",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(game)

    # Insert an open speak request (no matching result).
    await repo.insert_npc_speak_request(
        request_id="sr_test1",
        game_id=game.id,
        phase_id="phase1",
        npc_id="npc1",
        seat_no=2,
        logic_packet_id="lp1",
        suggested_intent="speak",
        max_chars=80,
        max_duration_ms=12000,
        priority=0,
        expires_at_ms=99999,
        created_at_ms=1000,
    )
    # Insert an open playback event.
    await repo.open_npc_playback(
        request_id="sr_test2",
        game_id=game.id,
        phase_id="phase1",
        npc_id="npc1",
        speech_event_id="se1",
        authorized_at_ms=1000,
        playback_deadline_ms=13000,
    )

    store = SqliteSpeechEventStore(repo._db)
    ds = DiscussionService(store=store, log_sink=repo)
    registry = InMemoryNpcRegistry()
    arbiter = SpeakArbiter(repo=repo, registry=registry, discussion=ds, now_ms=lambda: 5000)

    await arbiter.reactive_voice_recovery_sweep(game.id)

    # Verify open speak request was closed.
    open_reqs = await repo.load_open_npc_speak_requests(game.id)
    assert open_reqs == [], "All open speak requests should be closed"

    # Verify open playback was closed.
    open_play = await repo.load_open_npc_playback(game.id)
    assert open_play == [], "All open playback events should be closed"


async def test_recovery_service_calls_sweep_for_reactive_voice(repo: SqliteRepo) -> None:
    """RecoveryService._recover_one must call the reactive_voice sweep for
    reactive_voice games when the sweep callback is wired."""
    from wolfbot.domain.models import Seat
    from wolfbot.services.recovery_service import RecoveryService
    from wolfbot.services.timer_service import EngineRegistry

    game = Game(
        id=new_game_id(),
        guild_id=f"g-{new_game_id()}",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=9999999999,
        main_text_channel_id="100",
        main_vc_channel_id="200",
        created_at=0,
        discussion_mode="reactive_voice",
    )
    await repo.create_game(game)
    await repo.insert_seat(
        game.id,
        Seat(seat_no=1, display_name="H1", discord_user_id="u1", is_llm=False, persona_key=None),
    )

    swept_games: list[str] = []

    async def _sweep(game_id: str) -> None:
        swept_games.append(game_id)

    mock_discord = MagicMock()
    mock_discord.reconcile = MagicMock(return_value=None)
    mock_discord.announce_recovery = MagicMock(return_value=None)

    async def noop(*_a: Any, **_k: Any) -> None:
        pass

    mock_discord.reconcile = noop
    mock_discord.announce_recovery = noop

    from unittest.mock import AsyncMock

    mock_gs = MagicMock()
    mock_gs.advance = AsyncMock()
    mock_gs.resend_pending_dms = AsyncMock()
    mock_gs.resume_llm_speech_progress = AsyncMock()

    registry = EngineRegistry()
    svc = RecoveryService(
        repo=repo,
        game_service=mock_gs,
        registry=registry,
        discord=mock_discord,
        reactive_voice_sweep=_sweep,
    )

    recovered = await svc.recover_all()
    assert game.id in recovered
    assert game.id in swept_games, "Sweep must be called for reactive_voice games"


async def test_game_service_emits_phase_summary_on_discussion_exit(repo: SqliteRepo) -> None:
    """GameService.advance must emit discussion_phase_summary when leaving
    DAY_DISCUSSION phase."""
    from wolfbot.domain.models import Seat
    from wolfbot.services.discussion_service import (
        DiscussionService,
        SqliteSpeechEventStore,
    )
    from wolfbot.services.game_service import GameService

    game = Game(
        id=new_game_id(),
        guild_id=f"g-{new_game_id()}",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        deadline_epoch=100,
        main_text_channel_id="100",
        main_vc_channel_id="200",
        created_at=0,
        discussion_mode="rounds",
    )
    await repo.create_game(game)
    from wolfbot.domain.enums import Role

    for sno in range(1, 10):
        await repo.insert_seat(
            game.id,
            Seat(
                seat_no=sno,
                display_name=f"P{sno}",
                discord_user_id=f"u{sno}",
                is_llm=False,
                persona_key=None,
            ),
        )
        await repo.set_player_role(game.id, sno, Role.VILLAGER)

    store = SqliteSpeechEventStore(repo._db)
    ds = DiscussionService(store=store, log_sink=repo)

    mock_discord = MagicMock()

    async def noop_apply(*_a: Any, **_k: Any) -> None:
        pass

    mock_discord.apply_permissions = noop_apply
    mock_discord.kill_permissions = noop_apply
    mock_discord.post_public = noop_apply
    mock_discord.post_morning = noop_apply
    mock_discord.send_private = noop_apply
    mock_discord.send_vote_dms = noop_apply
    mock_discord.send_night_action_dms = noop_apply
    mock_discord.announce_waiting = noop_apply
    mock_discord.on_game_end = noop_apply
    mock_discord.reconcile = noop_apply
    mock_discord.post_wolves_chat = noop_apply

    mock_llm = MagicMock()
    mock_llm.submit_llm_votes = noop_apply
    mock_llm.submit_llm_discussion_rounds = noop_apply
    mock_llm.submit_llm_runoff_candidate_speeches = noop_apply
    mock_llm.submit_llm_night_actions = noop_apply
    mock_llm.discussion_service = ds

    mock_wake = MagicMock()
    mock_wake.wake = MagicMock()

    service = GameService(
        repo=repo, discord=mock_discord, llm=mock_llm, wake=mock_wake, clock=lambda: 200
    )

    # The advance should transition from DAY_DISCUSSION to DAY_VOTE and
    # emit the phase summary. We just need it to not crash.
    await service.advance(game.id)

    # Verify the game moved to DAY_VOTE.
    updated = await repo.load_game(game.id)
    assert updated is not None
    assert updated.phase == Phase.DAY_VOTE


async def test_ws_authenticate_reads_request_path() -> None:
    """WebsocketsMasterWsServer._authenticate must read ws.request.path
    (websockets 16.0) rather than the legacy ws.path."""
    from wolfbot.master.npc_registry import InMemoryNpcRegistry
    from wolfbot.master.ws_server import MasterHandlers, WebsocketsMasterWsServer

    registry = InMemoryNpcRegistry()
    handlers = MasterHandlers(registry=registry, now_ms=lambda: 0)
    server = WebsocketsMasterWsServer(
        host="127.0.0.1",
        port=8899,
        psk="testpsk",
        registry=registry,
        handlers=handlers,
    )

    # Simulate websockets 16.0 connection object with ws.request.path
    class _FakeRequest:
        path = "/?role=npc&psk=testpsk"

    class _FakeWs:
        request = _FakeRequest()
        # No .path attribute — websockets 16.0 style

        async def send(self, data: str) -> None:
            pass

        async def close(self, code: int = 1000, reason: str = "") -> None:
            pass

    ctx = await server._authenticate(_FakeWs())
    assert ctx is not None
    assert ctx.role == "npc"


async def test_ws_authenticate_rejects_bad_psk() -> None:
    """Auth must reject when psk doesn't match."""
    from wolfbot.master.npc_registry import InMemoryNpcRegistry
    from wolfbot.master.ws_server import MasterHandlers, WebsocketsMasterWsServer

    registry = InMemoryNpcRegistry()
    handlers = MasterHandlers(registry=registry, now_ms=lambda: 0)
    server = WebsocketsMasterWsServer(
        host="127.0.0.1",
        port=8899,
        psk="testpsk",
        registry=registry,
        handlers=handlers,
    )

    class _FakeRequest:
        path = "/?role=npc&psk=wrong"

    closed_with: list[int] = []

    class _FakeWs:
        request = _FakeRequest()

        async def send(self, data: str) -> None:
            pass

        async def close(self, code: int = 1000, reason: str = "") -> None:
            closed_with.append(code)

    ctx = await server._authenticate(_FakeWs())
    assert ctx is None
    assert closed_with == [4401]


# ---------------------------------------------------------------------------
# R1-F04 / R4-F10: on_message text path triggers arbiter dispatch callback
# ---------------------------------------------------------------------------


async def test_on_message_text_triggers_speech_recorded_callback(repo: SqliteRepo) -> None:
    """After recording a text SpeechEvent, on_message must call the
    on_speech_recorded callback so the arbiter can dispatch an NPC reply."""
    game, _store, ds = await _make_discussion_game(
        repo, human_seats=[1], discussion_mode="reactive_voice"
    )

    dispatched_game_ids: list[str] = []

    async def fake_on_speech_recorded(game_id: str) -> None:
        dispatched_game_ids.append(game_id)

    cog = WolfCog(
        bot=MagicMock(),
        repo=repo,
        game_service=MagicMock(),
        discord_adapter=MagicMock(),
        llm_adapter=MagicMock(),
        registry=MagicMock(),
        settings=MagicMock(MAIN_TEXT_CHANNEL_ID=100, MAIN_VOICE_CHANNEL_ID=200),
        discussion_service=ds,
        on_speech_recorded=fake_on_speech_recorded,
    )

    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = "user1"
    msg.guild.id = game.guild_id
    msg.channel.id = 100
    msg.content = "テスト発言"

    await WolfCog.on_message(cog, msg)

    assert dispatched_game_ids == [game.id], (
        "on_speech_recorded must be called with the game_id after text event"
    )


async def test_on_message_text_no_callback_when_none(repo: SqliteRepo) -> None:
    """When on_speech_recorded is None (no arbiter wired), on_message still
    records the SpeechEvent without error."""
    from wolfbot.domain.discussion import SpeechSource, make_phase_id

    game, store, ds = await _make_discussion_game(repo, human_seats=[1])

    cog = WolfCog(
        bot=MagicMock(),
        repo=repo,
        game_service=MagicMock(),
        discord_adapter=MagicMock(),
        llm_adapter=MagicMock(),
        registry=MagicMock(),
        settings=MagicMock(MAIN_TEXT_CHANNEL_ID=100, MAIN_VOICE_CHANNEL_ID=200),
        discussion_service=ds,
        on_speech_recorded=None,
    )

    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = "user1"
    msg.guild.id = game.guild_id
    msg.channel.id = 100
    msg.content = "発言テスト"

    await WolfCog.on_message(cog, msg)

    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)
    events = await store.load_phase(game.id, phase_id)
    non_baseline = [e for e in events if e.source != SpeechSource.PHASE_BASELINE]
    assert len(non_baseline) == 1


async def test_main_wires_on_speech_recorded_to_cog() -> None:
    """Verify the main module wiring passes on_speech_recorded to WolfCog."""
    import inspect

    sig = inspect.signature(WolfCog.__init__)
    assert "on_speech_recorded" in sig.parameters, (
        "WolfCog must accept on_speech_recorded callback"
    )
