"""Integration test: main-channel posts by dead players must not hit the log.

Regression for 2026-04-24 v5 review Medium #3. Discord permissions normally
stop a dead player from sending into the main channel, but as defence in
depth the bot must also refuse to persist their message as PLAYER_SPEECH —
otherwise a perm bypass (admin role, cached webhook) would let a dead seat
pollute every later LLM's public-log context via build_user_context().
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, PlayerUpdate, Seat, Transition
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discord_service import WolfCog


async def _seed_day_discussion_with_dead_player(repo: SqliteRepo) -> Game:
    game = Game(
        id="g1",
        guild_id="100",
        host_user_id="h1",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="main1",
        main_vc_channel_id="vc1",
        heaven_channel_id="heaven1",
        wolves_channel_id="wolves1",
        created_at=0,
    )
    await repo.create_game(game)
    await repo.insert_seat(
        game.id,
        Seat(seat_no=1, display_name="Alive", discord_user_id="u1", is_llm=False, persona_key=None),
    )
    await repo.insert_seat(
        game.id,
        Seat(seat_no=2, display_name="Dead", discord_user_id="u2", is_llm=False, persona_key=None),
    )
    await repo.set_player_role(game.id, 1, Role.VILLAGER)
    await repo.set_player_role(game.id, 2, Role.VILLAGER)
    # Mark seat 2 as dead via a minimal Transition — the death_day does not
    # matter for this test, only `alive=False`. expected_phase must match the
    # current DAY_DISCUSSION so the optimistic lock accepts the write.
    ok = await repo.apply_transition(
        game.id,
        Transition(
            next_phase=Phase.DAY_DISCUSSION,
            next_day=1,
            player_updates=(PlayerUpdate(seat_no=2, alive=False),),
        ),
        expected_phase=Phase.DAY_DISCUSSION,
    )
    assert ok
    loaded = await repo.load_game(game.id)
    assert loaded is not None
    return loaded


def _build_cog(repo: SqliteRepo) -> WolfCog:
    settings = MagicMock()
    settings.MAIN_TEXT_CHANNEL_ID = 100
    settings.MAIN_VOICE_CHANNEL_ID = 200
    return WolfCog(
        bot=MagicMock(),
        repo=repo,
        game_service=MagicMock(),
        discord_adapter=MagicMock(),
        llm_adapter=MagicMock(),
        registry=MagicMock(),
        settings=settings,
    )


def _fake_message(*, guild_id: str, channel_id: str, author_id: str, content: str) -> Any:
    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = author_id
    msg.guild = MagicMock()
    msg.guild.id = guild_id
    msg.channel.id = channel_id
    msg.content = content
    return msg


async def test_dead_player_main_post_does_not_insert_public_log(repo: SqliteRepo) -> None:
    game = await _seed_day_discussion_with_dead_player(repo)
    cog = _build_cog(repo)

    msg = _fake_message(
        guild_id="100", channel_id="main1", author_id="u2", content="I am dead, but listen!"
    )
    await cog.on_message(msg)

    logs = await repo.load_public_logs(game.id, limit=100)
    assert not [log for log in logs if log.get("kind") == "PLAYER_SPEECH"], (
        "dead player's DAY_DISCUSSION post leaked into logs_public"
    )


async def test_alive_player_main_post_still_inserts_public_log(repo: SqliteRepo) -> None:
    """Sanity check: the gate must not also silence living players."""
    game = await _seed_day_discussion_with_dead_player(repo)
    cog = _build_cog(repo)

    msg = _fake_message(
        guild_id="100", channel_id="main1", author_id="u1", content="朝になりました。"
    )
    await cog.on_message(msg)

    logs = await repo.load_public_logs(game.id, limit=100)
    speeches = [log for log in logs if log.get("kind") == "PLAYER_SPEECH"]
    assert len(speeches) == 1
    assert speeches[0].get("actor_seat") == 1


async def test_non_participant_main_post_does_not_insert_public_log(repo: SqliteRepo) -> None:
    """A spectator / admin with no seat must not be able to seed the log."""
    game = await _seed_day_discussion_with_dead_player(repo)
    cog = _build_cog(repo)

    msg = _fake_message(
        guild_id="100", channel_id="main1", author_id="u999", content="spectator comment"
    )
    await cog.on_message(msg)

    logs = await repo.load_public_logs(game.id, limit=100)
    assert not [log for log in logs if log.get("kind") == "PLAYER_SPEECH"]
