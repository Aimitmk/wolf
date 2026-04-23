"""Unit tests for DiscordBotAdapter methods that don't need a live discord.Client."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import Game, Player, Seat
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discord_service import DiscordBotAdapter
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
