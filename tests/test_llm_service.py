"""Focused tests for LLMAdapter's background-task dispatch behavior.

These verify that `submit_llm_votes` / `submit_llm_night_actions` are
fire-and-forget: the caller returns immediately even if the underlying
decider is slow, and in-flight tasks abort cleanly when the game's phase
advances or ends mid-iteration.
"""

from __future__ import annotations

import asyncio
import random

from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import Game, NightAction, Seat, Vote
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.llm_service import LLMAction, LLMAdapter


class _FakeGameService:
    def __init__(self) -> None:
        self.votes: list[tuple[str, int, int | None, int, int]] = []
        self.nights: list[tuple[str, int, SubmissionType, int | None, int]] = []

    async def submit_vote(
        self,
        game_id: str,
        voter_seat: int,
        target_seat: int | None,
        round_: int,
        day: int,
    ) -> None:
        self.votes.append((game_id, voter_seat, target_seat, round_, day))

    async def submit_night_action(
        self,
        game_id: str,
        actor_seat: int,
        kind: SubmissionType,
        target_seat: int | None,
        day: int,
    ) -> None:
        self.nights.append((game_id, actor_seat, kind, target_seat, day))


async def _seed_vote_game(repo: SqliteRepo) -> tuple[Game, list[Seat]]:
    game = Game(
        id="g-llm",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_VOTE,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        heaven_channel_id="h1",
        wolves_channel_id="w1",
        created_at=0,
    )
    await repo.create_game(game)
    seats = [
        Seat(seat_no=1, display_name="H1", discord_user_id="1001", is_llm=False, persona_key=None),
        Seat(
            seat_no=2, display_name="Setsu", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(seat_no=3, display_name="Gina", discord_user_id=None, is_llm=True, persona_key="gina"),
    ]
    for s in seats:
        await repo.insert_seat(game.id, s)
    await repo.set_player_role(game.id, 1, Role.VILLAGER)
    await repo.set_player_role(game.id, 2, Role.SEER)
    await repo.set_player_role(game.id, 3, Role.VILLAGER)
    return game, seats


class _BlockingDecider:
    """Waits on an asyncio.Event before returning each scripted action."""

    def __init__(self, actions: list[LLMAction], release: asyncio.Event) -> None:
        self._actions = actions
        self._release = release
        self.call_count = 0

    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        self.call_count += 1
        await self._release.wait()
        return self._actions.pop(0)


async def test_submit_llm_votes_returns_before_decider_completes(repo: SqliteRepo) -> None:
    """The public method must schedule work and return immediately; the
    background task is what actually blocks on xAI. If this regresses, a slow
    xAI will stall `GameService.advance()` and the engine's deadline monitor."""
    game, seats = await _seed_vote_game(repo)
    gs = _FakeGameService()
    release = asyncio.Event()
    decider = _BlockingDecider(
        actions=[
            LLMAction(intent="vote", target_name="H1", reason_summary="", confidence=0.5),
            LLMAction(intent="vote", target_name="H1", reason_summary="", confidence=0.5),
        ],
        release=release,
    )
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(gs)  # type: ignore[arg-type]

    players = await repo.load_players(game.id)

    # submit_llm_votes must return even though the decider is stuck.
    await asyncio.wait_for(
        adapter.submit_llm_votes(game, players, seats, candidates=None, round_=0),
        timeout=0.5,
    )
    # No votes submitted yet — the background task is blocked on `release`.
    assert gs.votes == []
    assert len(adapter._background_tasks) == 1

    # Release the decider; drain and verify both LLMs submitted.
    release.set()
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)
    assert len(gs.votes) == 2


async def test_run_votes_aborts_when_phase_advances_midway(repo: SqliteRepo) -> None:
    """If the game advances out of DAY_VOTE between per-player iterations
    (e.g. all humans submitted and a wake fired), later LLM iterations must
    stop submitting. Otherwise a stale vote lands in the next round."""
    game, seats = await _seed_vote_game(repo)
    gs = _FakeGameService()

    # After the first decider call, flip the game phase to DAY_DISCUSSION in
    # the DB so the second iteration's recheck aborts the task.
    flipped = False

    class _FlipDecider:
        call_count = 0

        async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
            nonlocal flipped
            self.call_count += 1
            if not flipped:
                flipped = True
                async with repo._db.execute(  # type: ignore[attr-defined]
                    "UPDATE games SET phase=? WHERE id=?",
                    (Phase.DAY_DISCUSSION.value, game.id),
                ):
                    pass
                await repo._db.commit()  # type: ignore[attr-defined]
            return LLMAction(intent="vote", target_name="H1", reason_summary="", confidence=0.5)

    adapter = LLMAdapter(repo=repo, decider=_FlipDecider(), rng=random.Random(0))
    adapter.set_game_service(gs)  # type: ignore[arg-type]

    players = await repo.load_players(game.id)
    await adapter.submit_llm_votes(game, players, seats, candidates=None, round_=0)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    # Exactly one submission — the first — because the second iteration sees
    # the phase has moved and aborts before calling the decider again.
    assert len(gs.votes) == 1


class _ScriptedDecider:
    def __init__(self, actions: list[LLMAction]) -> None:
        self._actions = actions
        self.call_count = 0

    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        self.call_count += 1
        return self._actions.pop(0)


async def test_submit_llm_votes_restricts_to_seats(repo: SqliteRepo) -> None:
    """Fix 1: resend_pending_dms passes restrict_to_seats so only the still-
    pending LLM seats are re-dispatched, not every alive LLM."""
    game, seats = await _seed_vote_game(repo)
    gs = _FakeGameService()
    decider = _ScriptedDecider(
        [LLMAction(intent="vote", target_name="席1 H1", reason_summary="", confidence=0.5)]
    )
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(gs)  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    # Both LLM seats (2,3) are alive, but restrict to only seat 3 — seat 2
    # should not be asked at all.
    await adapter.submit_llm_votes(
        game,
        players,
        seats,
        candidates=None,
        round_=0,
        restrict_to_seats=frozenset({3}),
    )
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert len(gs.votes) == 1
    assert gs.votes[0][1] == 3  # voter_seat
    assert decider.call_count == 1


async def test_submit_llm_votes_skips_already_submitted(repo: SqliteRepo) -> None:
    """Fix 1: when a seat already has a vote row (original task already won),
    the resend's in-loop guard skips it. No duplicate submission."""
    game, seats = await _seed_vote_game(repo)
    # Pre-insert a vote for seat 2 so the resend sees it as already-submitted.
    await repo.insert_vote(
        Vote(
            game_id=game.id,
            day=1,
            round=0,
            voter_seat=2,
            target_seat=1,
            submitted_at=0,
        )
    )
    gs = _FakeGameService()
    decider = _ScriptedDecider(
        [LLMAction(intent="vote", target_name="席1 H1", reason_summary="", confidence=0.5)]
    )
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(gs)  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    # Dispatch with both seats — seat 2 must be skipped (pre-existing row),
    # seat 3 must submit.
    await adapter.submit_llm_votes(
        game,
        players,
        seats,
        candidates=None,
        round_=0,
        restrict_to_seats=frozenset({2, 3}),
    )
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert len(gs.votes) == 1
    assert gs.votes[0][1] == 3
    assert decider.call_count == 1


async def _seed_night_game(
    repo: SqliteRepo, *, wolves_channel: str | None
) -> tuple[Game, list[Seat]]:
    game = Game(
        id="g-night",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.NIGHT,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        heaven_channel_id="h1",
        wolves_channel_id=wolves_channel,
        created_at=0,
    )
    await repo.create_game(game)
    seats = [
        Seat(seat_no=1, display_name="H1", discord_user_id="1001", is_llm=False, persona_key=None),
        Seat(
            seat_no=2,
            display_name="Wolf1",
            discord_user_id=None,
            is_llm=True,
            persona_key="setsu",
        ),
        Seat(
            seat_no=3,
            display_name="Wolf2",
            discord_user_id=None,
            is_llm=True,
            persona_key="gina",
        ),
        Seat(seat_no=4, display_name="V4", discord_user_id=None, is_llm=True, persona_key="sq"),
    ]
    for s in seats:
        await repo.insert_seat(game.id, s)
    await repo.set_player_role(game.id, 1, Role.VILLAGER)
    await repo.set_player_role(game.id, 2, Role.WEREWOLF)
    await repo.set_player_role(game.id, 3, Role.WEREWOLF)
    await repo.set_player_role(game.id, 4, Role.VILLAGER)
    return game, seats


class _FakePoster:
    def __init__(self) -> None:
        self.public: list[tuple[str, str, str]] = []
        self.wolves: list[tuple[str, str, str]] = []

    async def post_public(self, game: Game, text: str, kind: str) -> None:
        self.public.append((game.id, text, kind))

    async def post_wolves_chat(self, game: Game, text: str, kind: str) -> None:
        self.wolves.append((game.id, text, kind))


async def test_wolf_chat_fires_with_two_wolves(repo: SqliteRepo) -> None:
    """Fix 4: both LLM wolves post to the wolves channel before attacking; each
    post is logged to logs_private for every alive wolf audience."""
    game, seats = await _seed_night_game(repo, wolves_channel="w1")
    gs = _FakeGameService()
    poster = _FakePoster()
    decider = _ScriptedDecider(
        [
            # Wolf chat for seat 2
            LLMAction(
                intent="speak",
                public_message="席1 H1 を襲撃しよう",
                reason_summary="",
                confidence=0.8,
            ),
            # Wolf chat for seat 3
            LLMAction(
                intent="speak",
                public_message="賛成、席1 H1 で行こう",
                reason_summary="",
                confidence=0.8,
            ),
            # Attack for seat 2
            LLMAction(
                intent="night_action",
                target_name="席1 H1",
                reason_summary="",
                confidence=0.8,
            ),
            # Attack for seat 3
            LLMAction(
                intent="night_action",
                target_name="席1 H1",
                reason_summary="",
                confidence=0.8,
            ),
            # Villager seat 4 has no night action (role=VILLAGER) so _role_to_kind
            # returns (None, []) and we skip — no further decider calls needed.
        ]
    )
    adapter = LLMAdapter(repo=repo, decider=decider, message_poster=poster, rng=random.Random(0))
    adapter.set_game_service(gs)  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_night_actions(game, players, seats)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    # Both wolves posted to wolves channel
    assert len(poster.wolves) == 2
    assert "Wolf1" in poster.wolves[0][1] and "席1 H1" in poster.wolves[0][1]
    assert "Wolf2" in poster.wolves[1][1]

    # Each WOLF_CHAT post produced one private log row per alive wolf audience
    # (2 wolves alive × 2 posts = 4 rows).
    priv_for_seat2 = await repo.load_private_logs_for_audience(game.id, audience_seat=2, limit=40)
    wolf_chats_seat2 = [r for r in priv_for_seat2 if r.get("kind") == "WOLF_CHAT"]
    assert len(wolf_chats_seat2) == 2

    priv_for_seat3 = await repo.load_private_logs_for_audience(game.id, audience_seat=3, limit=40)
    wolf_chats_seat3 = [r for r in priv_for_seat3 if r.get("kind") == "WOLF_CHAT"]
    assert len(wolf_chats_seat3) == 2

    # Villager seat 4 must not see wolf chat
    priv_for_seat4 = await repo.load_private_logs_for_audience(game.id, audience_seat=4, limit=40)
    wolf_chats_seat4 = [r for r in priv_for_seat4 if r.get("kind") == "WOLF_CHAT"]
    assert wolf_chats_seat4 == []

    # Both wolves submitted attacks on seat 1
    attack_submissions = [n for n in gs.nights if n[2] is SubmissionType.WOLF_ATTACK]
    assert len(attack_submissions) == 2
    assert all(n[3] == 1 for n in attack_submissions)  # target_seat == 1


async def test_wolf_chat_skipped_with_single_wolf(repo: SqliteRepo) -> None:
    """Fix 4: only 1 alive wolf → no coordination needed, no posts."""
    game, seats = await _seed_night_game(repo, wolves_channel="w1")
    # Kill seat 3 (the second wolf) via raw SQL — no public kill helper exists
    # outside of apply_transition. Player state lives on the seats table.
    async with repo._db.execute(  # type: ignore[attr-defined]
        "UPDATE seats SET alive=0 WHERE game_id=? AND seat_no=?",
        (game.id, 3),
    ):
        pass
    await repo._db.commit()  # type: ignore[attr-defined]
    gs = _FakeGameService()
    poster = _FakePoster()
    decider = _ScriptedDecider(
        [
            LLMAction(
                intent="night_action",
                target_name="席1 H1",
                reason_summary="",
                confidence=0.8,
            ),
        ]
    )
    adapter = LLMAdapter(repo=repo, decider=decider, message_poster=poster, rng=random.Random(0))
    adapter.set_game_service(gs)  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_night_actions(game, players, seats)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert poster.wolves == []  # no wolf chat


async def test_wolf_chat_skipped_no_wolves_channel(repo: SqliteRepo) -> None:
    """Fix 4: no wolves channel configured → skip the coordination phase."""
    game, seats = await _seed_night_game(repo, wolves_channel=None)
    gs = _FakeGameService()
    poster = _FakePoster()
    decider = _ScriptedDecider(
        [
            LLMAction(
                intent="night_action",
                target_name="席1 H1",
                reason_summary="",
                confidence=0.8,
            ),
            LLMAction(
                intent="night_action",
                target_name="席1 H1",
                reason_summary="",
                confidence=0.8,
            ),
        ]
    )
    adapter = LLMAdapter(repo=repo, decider=decider, message_poster=poster, rng=random.Random(0))
    adapter.set_game_service(gs)  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_night_actions(game, players, seats)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert poster.wolves == []  # no wolf chat


async def test_run_night_actions_skips_seat_with_existing_action(repo: SqliteRepo) -> None:
    """Fix 1: night re-dispatch won't double-submit for a seat that already
    has a submission (unless it's in unresolved_seats for a wolf split)."""
    game, seats = await _seed_night_game(repo, wolves_channel=None)
    # Pre-insert a WOLF_ATTACK for seat 2.
    await repo.insert_night_action(
        NightAction(
            game_id=game.id,
            day=1,
            actor_seat=2,
            kind=SubmissionType.WOLF_ATTACK,
            target_seat=1,
            submitted_at=0,
        )
    )
    gs = _FakeGameService()
    decider = _ScriptedDecider(
        [
            # Only seat 3 should ask; seat 2 already submitted and is not in unresolved.
            LLMAction(
                intent="night_action",
                target_name="席1 H1",
                reason_summary="",
                confidence=0.8,
            ),
        ]
    )
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(gs)  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_night_actions(
        game,
        players,
        seats,
        restrict_to_seats=frozenset({2, 3}),
    )
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    # Exactly one new submission (seat 3). Seat 2's pre-existing row is not overwritten.
    assert len(gs.nights) == 1
    assert gs.nights[0][1] == 3
    assert decider.call_count == 1


async def test_daystart_speech_inserts_public_log(repo: SqliteRepo) -> None:
    """Fix 2: when an LLM speaks at daystart, the speech is persisted to
    logs_public as PLAYER_SPEECH so subsequent LLMs see it in context."""
    # Use DAY_DISCUSSION seed so _maybe_speak's phase checks pass.
    game = Game(
        id="g-day",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        heaven_channel_id="h1",
        wolves_channel_id="w1",
        created_at=0,
    )
    await repo.create_game(game)
    seats = [
        Seat(seat_no=1, display_name="H1", discord_user_id="1001", is_llm=False, persona_key=None),
        Seat(
            seat_no=2,
            display_name="Setsu",
            discord_user_id=None,
            is_llm=True,
            persona_key="setsu",
        ),
    ]
    for s in seats:
        await repo.insert_seat(game.id, s)
    await repo.set_player_role(game.id, 1, Role.VILLAGER)
    await repo.set_player_role(game.id, 2, Role.VILLAGER)

    poster = _FakePoster()
    decider = _ScriptedDecider(
        [
            LLMAction(
                intent="speak",
                public_message="おはようございます",
                reason_summary="",
                confidence=0.5,
            ),
        ]
    )
    adapter = LLMAdapter(
        repo=repo,
        decider=decider,
        message_poster=poster,
        rng=random.Random(0),
        clock=lambda: 123,
    )
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)
    llm_player = next(p for p in players if p.seat_no == 2)
    llm_seat = next(s for s in seats if s.seat_no == 2)

    await adapter._maybe_speak(game, llm_player, llm_seat, seats)

    assert len(poster.public) == 1
    assert "Setsu" in poster.public[0][1] and "おはようございます" in poster.public[0][1]

    # Speech was persisted with actor_seat set so build_user_context can attribute it.
    logs = await repo.load_public_logs(game.id, limit=40)
    speech_rows = [r for r in logs if r.get("kind") == "PLAYER_SPEECH"]
    assert len(speech_rows) == 1
    assert speech_rows[0].get("actor_seat") == 2
    assert speech_rows[0].get("text") == "おはようございます"
