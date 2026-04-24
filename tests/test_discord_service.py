"""Unit tests for DiscordBotAdapter methods that don't need a live discord.Client."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import discord

from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import Game, Player, Seat
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discord_service import DiscordBotAdapter, WolfCog
from wolfbot.ui.views import NightActionView


class _CapturingAdapter(DiscordBotAdapter):
    """DiscordBotAdapter with _fetch_user replaced by a capture stub.

    Bypasses __init__ so we don't need a real discord.Client / Settings /
    PermissionManager to exercise send_night_action_dms.
    """

    def __init__(self, repo: SqliteRepo) -> None:
        self.bot = MagicMock()
        self.repo = repo
        self.settings = MagicMock()
        self.perms = MagicMock()
        # NightActionView needs a submit callback even if we never click it.
        gs_stub = MagicMock()
        gs_stub.submit_night_action = MagicMock()
        self._gs_slot = {"gs": gs_stub}
        self.captured: list[tuple[int, str, Any]] = []

    async def _fetch_user(self, user_id: int) -> Any:
        captured = self.captured

        class _User:
            async def send(self, text: str, view: Any = None) -> None:
                captured.append((user_id, text, view))

        return _User()


def _seats_human_wolves(wolf_seats: set[int]) -> list[Seat]:
    out: list[Seat] = []
    for i in range(1, 10):
        # All humans (non-LLM) so DMs fire for every actor we want to test.
        # discord_user_id must be numeric (snowflake); adapter calls int(...) on it.
        out.append(
            Seat(
                seat_no=i,
                display_name=f"P{i}",
                discord_user_id=str(1000 + i),
                is_llm=False,
                persona_key=None,
            )
        )
    return out


async def test_send_night_action_dms_uses_alive_pool_for_candidates(repo: SqliteRepo) -> None:
    """Actors is a subset (single wolf), but legal targets must be computed
    from the full alive pool — so the split-wolf resend sees 5 non-wolf targets."""
    game = Game(
        id="g-night",
        guild_id="g",
        host_user_id="h",
        phase=Phase.NIGHT,
        day_number=1,
        main_text_channel_id="ch-text",
        main_vc_channel_id="ch-vc",
        heaven_channel_id="ch-heaven",
        wolves_channel_id="ch-wolves",
        created_at=0,
    )
    await repo.create_game(game)
    seats = _seats_human_wolves(wolf_seats={1, 2})
    for s in seats:
        await repo.insert_seat(game.id, s)

    alive_players = [
        Player(
            seat_no=i,
            role=(Role.WEREWOLF if i in (1, 2) else Role.VILLAGER),
            alive=True,
        )
        for i in range(1, 10)
    ]
    # Resend scenario: only one of the two split wolves is re-DMed.
    actors = [alive_players[0]]  # seat 1 only

    adapter = _CapturingAdapter(repo)
    await adapter.send_night_action_dms(game, actors, alive_players, seats)

    # Exactly one DM went out (to wolf1).
    assert len(adapter.captured) == 1
    user_id, _, view = adapter.captured[0]
    assert user_id == 1001  # seat 1
    assert isinstance(view, NightActionView)
    assert view.kind is SubmissionType.WOLF_ATTACK
    # Legal targets = alive ∧ not self ∧ not wolf → seats 3..9 (7 candidates),
    # but the rules module excludes *all* werewolves, so seat 2 is excluded too.
    # Expected: {3,4,5,6,7,8,9}.
    option_seats = {int(opt.value) for opt in view.select_target.options}
    assert option_seats == {3, 4, 5, 6, 7, 8, 9}


async def test_send_night_action_dms_seer_with_filtered_actors_uses_full_alive(
    repo: SqliteRepo,
) -> None:
    """If only the seer is re-DMed (after wolves submitted), divine candidates
    must still include all alive survivors (wolves included), not just the
    seer alone."""
    game = Game(
        id="g-seer",
        guild_id="g",
        host_user_id="h",
        phase=Phase.NIGHT,
        day_number=1,
        main_text_channel_id="ch-text",
        main_vc_channel_id="ch-vc",
        heaven_channel_id="ch-heaven",
        wolves_channel_id="ch-wolves",
        created_at=0,
    )
    await repo.create_game(game)
    seats = _seats_human_wolves(wolf_seats={1, 2})
    for s in seats:
        await repo.insert_seat(game.id, s)

    # Seat 4 = seer.
    alive_players: list[Player] = []
    for i in range(1, 10):
        if i in (1, 2):
            role = Role.WEREWOLF
        elif i == 4:
            role = Role.SEER
        else:
            role = Role.VILLAGER
        alive_players.append(Player(seat_no=i, role=role, alive=True))
    actors = [alive_players[3]]  # seat 4 (seer) only

    adapter = _CapturingAdapter(repo)
    await adapter.send_night_action_dms(game, actors, alive_players, seats)

    assert len(adapter.captured) == 1
    _, _, view = adapter.captured[0]
    assert isinstance(view, NightActionView)
    assert view.kind is SubmissionType.SEER_DIVINE
    option_seats = {int(opt.value) for opt in view.select_target.options}
    # Divine = alive ∧ not self → seats {1,2,3,5,6,7,8,9}
    assert option_seats == {1, 2, 3, 5, 6, 7, 8, 9}


class _ChannelCapturingAdapter(DiscordBotAdapter):
    """DiscordBotAdapter with bot.get_channel wired to two mock channels.

    Captures text sent to the main-text channel vs. the wolves channel so we
    can assert that announce_waiting censors wolf identities on the public
    side while still relaying them to the wolves-only channel.
    """

    MAIN_ID = "ch-main"
    WOLVES_ID = "ch-wolves"

    def __init__(self, repo: SqliteRepo) -> None:
        self.bot = MagicMock()
        self.repo = repo
        self.settings = MagicMock()
        self.perms = MagicMock()
        self._gs_slot = {"gs": MagicMock()}
        self.main_sent: list[str] = []
        self.wolves_sent: list[str] = []

        test_self = self

        class _MainChannel:
            async def send(self, text: str) -> None:
                test_self.main_sent.append(text)

        class _WolvesChannel:
            async def send(self, text: str) -> None:
                test_self.wolves_sent.append(text)

        main = _MainChannel()
        wolves = _WolvesChannel()

        # discord.py isinstance check uses discord.TextChannel; patch the
        # wolf_service module-level reference to our duck-typed objects.
        import wolfbot.services.discord_service as mod

        test_self._main_obj = main  # type: ignore[attr-defined]
        test_self._wolves_obj = wolves  # type: ignore[attr-defined]

        def _get_channel(cid: int) -> object:
            if cid == int(_ChannelCapturingAdapter.MAIN_ID.replace("ch-main", "1")):
                return main
            if cid == int(_ChannelCapturingAdapter.WOLVES_ID.replace("ch-wolves", "2")):
                return wolves
            return None

        self.bot.get_channel = _get_channel

        # Bypass the isinstance(TextChannel) guard by monkey-patching the
        # adapter helpers on this instance.
        self._main_text = lambda game: main  # type: ignore[assignment]
        self._wolves_channel = lambda game: wolves  # type: ignore[assignment]
        # Silence unused-import warning for `mod`
        _ = mod


async def test_announce_waiting_censors_wolf_attack_names_on_main_channel(
    repo: SqliteRepo,
) -> None:
    """Fix 3 regression: WOLF_ATTACK split seat names MUST NOT appear in the
    main public channel; they must go to the wolves-only channel instead.

    Background: the old code posted
        `WOLF_ATTACK 再提出待ち(意見が割れました): Alice、Bob`
    directly to the main text channel, revealing both wolves to villagers.
    """
    from wolfbot.domain.models import PendingDecision, PendingSubmission

    game = Game(
        id="g-ann",
        guild_id="g",
        host_user_id="h",
        phase=Phase.WAITING_HOST_DECISION,
        day_number=1,
        main_text_channel_id="1",
        main_vc_channel_id="vc",
        heaven_channel_id="h",
        wolves_channel_id="2",
        created_at=0,
    )
    await repo.create_game(game)
    seats = _seats_human_wolves(wolf_seats={1, 2})
    # Rename seats 1 and 2 so we can grep the output for those names.
    seats[0] = Seat(
        seat_no=1, display_name="Alice", discord_user_id="1001", is_llm=False, persona_key=None
    )
    seats[1] = Seat(
        seat_no=2, display_name="Bob", discord_user_id="1002", is_llm=False, persona_key=None
    )
    for s in seats:
        await repo.insert_seat(game.id, s)

    pending = PendingDecision(
        game_id=game.id,
        phase=Phase.NIGHT,
        day=1,
        required_submission=SubmissionType.WOLF_ATTACK,
        missing_seats=(1, 2),
        submissions=(
            PendingSubmission(
                submission_type=SubmissionType.WOLF_ATTACK,
                missing_seats=(),
                unresolved_seats=(1, 2),
            ),
        ),
        created_at=0,
    )

    adapter = _ChannelCapturingAdapter(repo)
    await adapter.announce_waiting(game, pending, seats)

    # Public channel got the count-only version.
    assert len(adapter.main_sent) == 1
    main_text = adapter.main_sent[0]
    assert "2件" in main_text
    assert "Alice" not in main_text
    assert "Bob" not in main_text
    # Public wording must not reveal it was a *split* vs a plain no-submit —
    # "意見が割れました" implies ≥2 disagreeing wolves and leaks the count.
    assert "意見が割れました" not in main_text
    assert "未確定" in main_text

    # Wolves channel got the name-inclusive version (including split wording).
    assert len(adapter.wolves_sent) == 1
    wolves_text = adapter.wolves_sent[0]
    assert "Alice" in wolves_text and "Bob" in wolves_text


async def test_announce_waiting_vote_pending_retains_names(repo: SqliteRepo) -> None:
    """Fix 3 non-regression: DAY_VOTE pending lists still include seat names
    (who's voting is public info). No wolves-channel post."""
    from wolfbot.domain.models import PendingDecision, PendingSubmission

    game = Game(
        id="g-ann-v",
        guild_id="g",
        host_user_id="h",
        phase=Phase.WAITING_HOST_DECISION,
        day_number=1,
        main_text_channel_id="1",
        main_vc_channel_id="vc",
        heaven_channel_id="h",
        wolves_channel_id="2",
        created_at=0,
    )
    await repo.create_game(game)
    seats = _seats_human_wolves(wolf_seats=set())
    seats[0] = Seat(
        seat_no=1, display_name="Alice", discord_user_id="1001", is_llm=False, persona_key=None
    )
    for s in seats:
        await repo.insert_seat(game.id, s)

    pending = PendingDecision(
        game_id=game.id,
        phase=Phase.DAY_VOTE,
        day=1,
        required_submission=SubmissionType.VOTE,
        missing_seats=(1,),
        submissions=(
            PendingSubmission(
                submission_type=SubmissionType.VOTE,
                missing_seats=(1,),
            ),
        ),
        created_at=0,
    )

    adapter = _ChannelCapturingAdapter(repo)
    await adapter.announce_waiting(game, pending, seats)

    assert len(adapter.main_sent) == 1
    assert "Alice" in adapter.main_sent[0]
    # No wolves-channel post for pure vote-pending state.
    assert adapter.wolves_sent == []


class _ProbeUser:
    """Minimal duck-typed discord.User for _preflight_dms probing.

    Controls whether create_dm / send succeed so tests can exercise the two
    failure modes (channel open OK, send rejected) independently.
    """

    def __init__(self, *, fail_create: bool = False, fail_send: bool = False) -> None:
        self.fail_create = fail_create
        self.fail_send = fail_send
        self.sent: list[str] = []

    async def create_dm(self) -> None:
        if self.fail_create:
            raise discord.DiscordException("create_dm failed")

    async def send(self, text: str) -> None:
        if self.fail_send:
            raise discord.DiscordException("send failed")
        self.sent.append(text)


def _preflight_cog(users: dict[int, _ProbeUser]) -> WolfCog:
    """Build a WolfCog with just enough state for _preflight_dms to run.

    Bypasses __init__ since _preflight_dms only needs self.bot.get_user /
    self.bot.fetch_user; wiring up a real bot / repo / adapters would dwarf
    the test.
    """
    cog: WolfCog = object.__new__(WolfCog)
    cog.bot = MagicMock()  # type: ignore[attr-defined]
    cog.bot.get_user = lambda uid: users.get(int(uid))  # type: ignore[attr-defined]
    return cog


async def test_preflight_dms_rejects_when_send_fails() -> None:
    """create_dm() can succeed while send() is rejected by DM privacy settings.

    Preflight must probe send() so a preflight-pass can't hide a player whose
    post-start role/vote/night DMs will never arrive.
    """
    seats = [
        Seat(
            seat_no=1,
            display_name="Alice",
            discord_user_id="1001",
            is_llm=False,
            persona_key=None,
        ),
        Seat(
            seat_no=2,
            display_name="Bob",
            discord_user_id="1002",
            is_llm=False,
            persona_key=None,
        ),
    ]
    users = {
        1001: _ProbeUser(),
        1002: _ProbeUser(fail_send=True),
    }
    cog = _preflight_cog(users)

    failures = await cog._preflight_dms(seats)

    assert failures == ["Bob"]
    # Alice must have actually received the probe message — not just had her DM opened.
    assert users[1001].sent == ["人狼bot DM疎通確認です。まもなく役職をお伝えします。"]


async def test_preflight_dms_passes_when_send_succeeds() -> None:
    """Happy path: both create_dm and send succeed, no failures reported."""
    seats = [
        Seat(
            seat_no=1,
            display_name="Alice",
            discord_user_id="1001",
            is_llm=False,
            persona_key=None,
        ),
    ]
    users = {1001: _ProbeUser()}
    cog = _preflight_cog(users)

    failures = await cog._preflight_dms(seats)

    assert failures == []
    assert users[1001].sent == ["人狼bot DM疎通確認です。まもなく役職をお伝えします。"]
