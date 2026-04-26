"""Focused tests for LLMAdapter's background-task dispatch behavior.

These verify that `submit_llm_votes` / `submit_llm_night_actions` are
fire-and-forget: the caller returns immediately even if the underlying
decider is slow, and in-flight tasks abort cleanly when the game's phase
advances or ends mid-iteration.
"""

from __future__ import annotations

import asyncio
import random
import re

import pytest

from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import Game, LogEntry, NightAction, Seat, Vote
from wolfbot.llm.prompt_builder import task_daytime_speech, task_night_action, task_vote
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
            seat_no=2, display_name="セツ", discord_user_id=None, is_llm=True, persona_key="setsu"
        ),
        Seat(seat_no=3, display_name="ジナ", discord_user_id=None, is_llm=True, persona_key="gina"),
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


async def test_run_votes_aborts_when_phase_stale_at_dispatch(repo: SqliteRepo) -> None:
    """Per-seat stale check: if the phase is no longer DAY_VOTE when a sub-task
    reads the game, that sub-task returns without calling the decider. Parallel
    dispatch keeps the check at the top of each per-seat task so the guarantee
    still holds when all sub-tasks race each other."""
    game, seats = await _seed_vote_game(repo)
    # Pre-flip the phase so every parallel sub-task sees the stale state at
    # its own stale check.
    async with repo._db.execute(  # type: ignore[attr-defined]
        "UPDATE games SET phase=? WHERE id=?",
        (Phase.DAY_DISCUSSION.value, game.id),
    ):
        pass
    await repo._db.commit()  # type: ignore[attr-defined]

    gs = _FakeGameService()
    decider = _ScriptedDecider([])  # must not be called
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(gs)  # type: ignore[arg-type]

    players = await repo.load_players(game.id)
    await adapter.submit_llm_votes(game, players, seats, candidates=None, round_=0)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert gs.votes == []
    assert decider.call_count == 0


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


class _WolfChatGuardDecider:
    """Blocks inside _ask until the test releases, signals entry via an event.

    The entry event lets the test wait until a wolf is reliably stuck inside
    `_ask` before flipping the game phase — so we deterministically exercise
    the post-`_ask` guard rather than the pre-`_ask` one.
    """

    def __init__(
        self,
        action: LLMAction,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._action = action
        self.entered = entered
        self.release = release
        self.call_count = 0

    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        self.call_count += 1
        self.entered.set()
        await self.release.wait()
        return self._action


async def test_wolf_chat_skipped_when_phase_advances_during_ask(repo: SqliteRepo) -> None:
    """Post-ask stale guard: if deadline / force-skip / abort / victory moves
    the game on while _ask() is awaiting the LLM, the wolf-chat post and its
    private log must be suppressed — otherwise a stale speech lands in the
    wolves channel after night has ended.
    """
    game, seats = await _seed_night_game(repo, wolves_channel="w1")
    gs = _FakeGameService()
    poster = _FakePoster()
    entered = asyncio.Event()
    release = asyncio.Event()
    decider = _WolfChatGuardDecider(
        action=LLMAction(
            intent="speak",
            public_message="席1 H1 を襲撃しよう",
            reason_summary="",
            confidence=0.8,
        ),
        entered=entered,
        release=release,
    )
    adapter = LLMAdapter(repo=repo, decider=decider, message_poster=poster, rng=random.Random(0))
    adapter.set_game_service(gs)  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    # Kick off the night pipeline. `_run_wolf_chat` calls `_ask` for wolf A,
    # which blocks on `release` inside the decider.
    await adapter.submit_llm_night_actions(game, players, seats)
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    # Simulate force-skip / deadline advance while the LLM is still thinking.
    async with repo._db.execute(  # type: ignore[attr-defined]
        "UPDATE games SET phase=? WHERE id=?",
        (Phase.DAY_DISCUSSION.value, game.id),
    ):
        pass
    await repo._db.commit()  # type: ignore[attr-defined]

    release.set()
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    # Decider was called exactly once (wolf A). Post-ask guard must have
    # short-circuited before posting; wolf B must never be asked because
    # the outer loop returns on stale.
    assert decider.call_count == 1
    assert poster.wolves == []

    # No WOLF_CHAT private log was written for either wolf audience.
    priv_for_seat2 = await repo.load_private_logs_for_audience(game.id, audience_seat=2, limit=40)
    priv_for_seat3 = await repo.load_private_logs_for_audience(game.id, audience_seat=3, limit=40)
    assert [r for r in priv_for_seat2 if r.get("kind") == "WOLF_CHAT"] == []
    assert [r for r in priv_for_seat3 if r.get("kind") == "WOLF_CHAT"] == []


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


class _ConcurrencyDecider:
    """Tracks peak concurrent `decide` calls. Distinguishes serial vs parallel
    dispatch: a serial-for-loop invoker reaches the decider one seat at a time
    (peak == 1); a parallel invoker lets multiple coroutines share the sleep
    window (peak >= 2)."""

    def __init__(self, result: LLMAction) -> None:
        self._result = result
        self.active = 0
        self.peak = 0
        self.call_count = 0

    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        self.call_count += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            # Enough yields for peer sub-tasks to reach this method too under
            # parallel dispatch; harmless under serial.
            await asyncio.sleep(0.02)
            return self._result
        finally:
            self.active -= 1


async def _seed_many_vote_game(repo: SqliteRepo, *, llm_seats: int = 4) -> tuple[Game, list[Seat]]:
    """DAY_VOTE game with 1 human target + N LLM voters. Everyone is a VILLAGER so
    `_role_to_kind` is irrelevant; we only exercise vote dispatch."""
    game = Game(
        id="g-many",
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
    persona_keys = ["setsu", "gina", "sq", "raqio", "stella", "shigemichi"]
    assert llm_seats <= len(persona_keys)
    seats: list[Seat] = [
        Seat(
            seat_no=1,
            display_name="H1",
            discord_user_id="u1",
            is_llm=False,
            persona_key=None,
        ),
    ]
    for i in range(llm_seats):
        seat_no = 2 + i
        seats.append(
            Seat(
                seat_no=seat_no,
                display_name=f"L{seat_no}",
                discord_user_id=None,
                is_llm=True,
                persona_key=persona_keys[i],
            )
        )
    for s in seats:
        await repo.insert_seat(game.id, s)
    for s in seats:
        await repo.set_player_role(game.id, s.seat_no, Role.VILLAGER)
    return game, seats


async def test_run_votes_dispatches_llms_in_parallel(repo: SqliteRepo) -> None:
    """Per-seat vote dispatch is parallel, not serial. With 4 LLM voters, the
    peak concurrent `decide` call count must exceed 1 (serial) — at least 2 are
    in-flight simultaneously."""
    game, seats = await _seed_many_vote_game(repo, llm_seats=4)
    gs = _FakeGameService()
    decider = _ConcurrencyDecider(
        LLMAction(intent="vote", target_name="席1 H1", reason_summary="", confidence=0.5)
    )
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(gs)  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_votes(game, players, seats, candidates=None, round_=0)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert decider.call_count == 4
    assert len(gs.votes) == 4
    # Serial dispatch would have peak==1; parallel dispatch lets all 4 sub-tasks
    # share the decider sleep. Assert >= 2 to tolerate scheduler jitter, with
    # the practical expectation of 4.
    assert decider.peak >= 2, f"expected parallel dispatch (peak>=2), saw peak={decider.peak}"


async def _seed_many_night_game(
    repo: SqliteRepo, *, wolves_channel: str | None = None
) -> tuple[Game, list[Seat]]:
    """NIGHT with 3 LLM actors (wolf, seer, knight) + 1 human villager. No wolves
    channel by default so `_run_wolf_chat` exits early and we only measure the
    post-coordination attack/seer/knight parallel phase."""
    game = Game(
        id="g-multi-night",
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
        Seat(
            seat_no=1,
            display_name="H1",
            discord_user_id="u1",
            is_llm=False,
            persona_key=None,
        ),
        Seat(
            seat_no=2,
            display_name="Wolf",
            discord_user_id=None,
            is_llm=True,
            persona_key="setsu",
        ),
        Seat(
            seat_no=3,
            display_name="Seer",
            discord_user_id=None,
            is_llm=True,
            persona_key="gina",
        ),
        Seat(
            seat_no=4,
            display_name="Knight",
            discord_user_id=None,
            is_llm=True,
            persona_key="sq",
        ),
    ]
    for s in seats:
        await repo.insert_seat(game.id, s)
    await repo.set_player_role(game.id, 1, Role.VILLAGER)
    await repo.set_player_role(game.id, 2, Role.WEREWOLF)
    await repo.set_player_role(game.id, 3, Role.SEER)
    await repo.set_player_role(game.id, 4, Role.KNIGHT)
    return game, seats


async def test_run_night_actions_dispatches_llms_in_parallel(repo: SqliteRepo) -> None:
    """Per-seat night-action dispatch runs sub-tasks concurrently after the
    wolf-chat prelude. Wolf (seat 2), Seer (seat 3), Knight (seat 4) all asked
    in parallel."""
    game, seats = await _seed_many_night_game(repo, wolves_channel=None)
    gs = _FakeGameService()
    decider = _ConcurrencyDecider(
        LLMAction(
            intent="night_action",
            target_name="席1 H1",
            reason_summary="",
            confidence=0.8,
        )
    )
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(gs)  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_night_actions(game, players, seats)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert decider.call_count == 3
    assert len(gs.nights) == 3
    assert decider.peak >= 2, f"expected parallel dispatch (peak>=2), saw peak={decider.peak}"


class _CapturingDecider:
    """Records (system, user) prompts for inspection; returns a fixed skip."""

    def __init__(self) -> None:
        self.captured: list[tuple[str, str]] = []

    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        self.captured.append((system_prompt, user_context))
        return LLMAction(intent="skip", reason_summary="captured", confidence=0.0)


def _game_with_id(game_id: str, *, guild_id: str = "gu", wolves_channel: str = "w1") -> Game:
    return Game(
        id=game_id,
        guild_id=guild_id,
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        heaven_channel_id="h1",
        wolves_channel_id=wolves_channel,
        created_at=0,
    )


async def test_ask_scopes_logs_to_current_game_id(repo: SqliteRepo) -> None:
    """Regression test: an LLM asked for game B must never see logs from game A,
    even when both games share guild / seat numbers / persona keys. This locks in
    the invariant that `load_public_logs` / `load_private_logs_for_audience` are
    scoped by `game_id` and that `_ask` never reaches for Discord channel history.
    """
    seats = [
        Seat(
            seat_no=1,
            display_name="H1",
            discord_user_id="u1",
            is_llm=False,
            persona_key=None,
        ),
        Seat(
            seat_no=2,
            display_name="L2",
            discord_user_id=None,
            is_llm=True,
            persona_key="setsu",
        ),
    ]

    game_a = _game_with_id("game-alpha")
    await repo.create_game(game_a)
    for s in seats:
        await repo.insert_seat(game_a.id, s)
    await repo.set_player_role(game_a.id, 1, Role.VILLAGER)
    await repo.set_player_role(game_a.id, 2, Role.SEER)
    # Game A distinctive logs — must NOT leak into game B's prompt.
    await repo.insert_log_public(
        LogEntry(
            game_id=game_a.id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            kind="PLAYER_SPEECH",
            actor_seat=1,
            visibility="PUBLIC",
            text="LEAK_A_PUBLIC_SECRET",
            created_at=1,
        )
    )
    await repo.insert_log_private(
        LogEntry(
            game_id=game_a.id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            kind="SEER_RESULT",
            actor_seat=None,
            audience_seat=2,
            visibility="PRIVATE",
            text="LEAK_A_PRIVATE_SECRET",
            created_at=1,
        )
    )

    # End game A so its channels would normally be torn down. The test doesn't
    # need the teardown to have run — the DB-level scoping is what we verify.
    await repo.end_game(game_a.id, ended_at_epoch=2)

    # Same guild, same seat layout, same persona key — only game_id differs.
    game_b = _game_with_id("game-beta")
    await repo.create_game(game_b)
    for s in seats:
        await repo.insert_seat(game_b.id, s)
    await repo.set_player_role(game_b.id, 1, Role.VILLAGER)
    await repo.set_player_role(game_b.id, 2, Role.SEER)
    await repo.insert_log_public(
        LogEntry(
            game_id=game_b.id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            kind="PLAYER_SPEECH",
            actor_seat=1,
            visibility="PUBLIC",
            text="GAMEB_PUBLIC_OK",
            created_at=10,
        )
    )
    await repo.insert_log_private(
        LogEntry(
            game_id=game_b.id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            kind="SEER_RESULT",
            actor_seat=None,
            audience_seat=2,
            visibility="PRIVATE",
            text="GAMEB_PRIVATE_OK",
            created_at=10,
        )
    )

    decider = _CapturingDecider()
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players_b = await repo.load_players(game_b.id)
    me = next(p for p in players_b if p.seat_no == 2)
    my_seat = next(s for s in seats if s.seat_no == 2)

    await adapter._ask(game_b, me, my_seat, players_b, seats, task_text="test")

    assert len(decider.captured) == 1
    _, user_prompt = decider.captured[0]
    assert "GAMEB_PUBLIC_OK" in user_prompt
    assert "GAMEB_PRIVATE_OK" in user_prompt
    assert "LEAK_A_PUBLIC_SECRET" not in user_prompt
    assert "LEAK_A_PRIVATE_SECRET" not in user_prompt


async def test_load_public_logs_isolated_by_game_id(repo: SqliteRepo) -> None:
    """Repo-level invariant: logs loaded for game A never include game B's rows.
    Companion to `test_ask_scopes_logs_to_current_game_id` — this exercises the
    SQL `WHERE game_id=?` filter directly.

    Uses distinct guild_ids so both games can be active concurrently (the DB
    enforces at-most-one active game per guild)."""
    game_a = _game_with_id("repo-alpha", guild_id="guild-alpha")
    game_b = _game_with_id("repo-beta", guild_id="guild-beta")
    await repo.create_game(game_a)
    await repo.create_game(game_b)
    for g, marker in ((game_a, "ONLY_ALPHA"), (game_b, "ONLY_BETA")):
        await repo.insert_log_public(
            LogEntry(
                game_id=g.id,
                day=1,
                phase=Phase.DAY_DISCUSSION,
                kind="PLAYER_SPEECH",
                actor_seat=1,
                visibility="PUBLIC",
                text=marker,
                created_at=1,
            )
        )
        await repo.insert_log_private(
            LogEntry(
                game_id=g.id,
                day=1,
                phase=Phase.DAY_DISCUSSION,
                kind="SEER_RESULT",
                actor_seat=None,
                audience_seat=2,
                visibility="PRIVATE",
                text=f"PRIV_{marker}",
                created_at=1,
            )
        )

    pub_a = await repo.load_public_logs(game_a.id)
    pub_b = await repo.load_public_logs(game_b.id)
    assert all(r["text"] != "ONLY_BETA" for r in pub_a)
    assert any(r["text"] == "ONLY_ALPHA" for r in pub_a)
    assert all(r["text"] != "ONLY_ALPHA" for r in pub_b)
    assert any(r["text"] == "ONLY_BETA" for r in pub_b)

    priv_a = await repo.load_private_logs_for_audience(game_a.id, audience_seat=2)
    priv_b = await repo.load_private_logs_for_audience(game_b.id, audience_seat=2)
    assert all(r["text"] != "PRIV_ONLY_BETA" for r in priv_a)
    assert any(r["text"] == "PRIV_ONLY_ALPHA" for r in priv_a)
    assert all(r["text"] != "PRIV_ONLY_ALPHA" for r in priv_b)
    assert any(r["text"] == "PRIV_ONLY_BETA" for r in priv_b)


async def test_discussion_speech_inserts_public_log(repo: SqliteRepo) -> None:
    """When an LLM speaks during DAY_DISCUSSION, the speech is persisted to
    logs_public as PLAYER_SPEECH so subsequent LLMs see it in context."""
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
            display_name="セツ",
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

    await adapter._do_one_discussion_speech(
        game=game, player=llm_player, seat=llm_seat, seats=seats
    )

    assert len(poster.public) == 1
    assert "セツ" in poster.public[0][1] and "おはようございます" in poster.public[0][1]

    # Speech was persisted with actor_seat set so build_user_context can attribute it.
    logs = await repo.load_public_logs(game.id, limit=40)
    speech_rows = [r for r in logs if r.get("kind") == "PLAYER_SPEECH"]
    assert len(speech_rows) == 1
    assert speech_rows[0].get("actor_seat") == 2
    assert speech_rows[0].get("text") == "おはようございます"


# ---------------------------------- system prompt enrichment via _ask
async def _capture_ask_system_prompt(
    repo: SqliteRepo,
    role: Role,
    *,
    persona_key: str = "setsu",
    task_text: str = "test-task",
) -> str:
    """Seed a tiny game with one LLM seat of the given role, invoke `_ask`,
    and return the captured system prompt. The role-specific strategy and the
    shared rules block are injected inside `build_system_prompt`, which `_ask`
    calls per-seat, so this exercises the exact production path.

    `persona_key` lets callers capture the prompt for any persona; the default
    preserves behavior for all pre-existing callers. `task_text` flows into the
    `{task_block}` slot of the system prompt — pass an actual `task_night_action`
    / `task_wolf_chat` string to assert task-specific content reaches the LLM.
    Both `game_id` and `guild_id` are scoped by (role, persona_key) so multiple
    calls in one test don't collide on the shared repo or trip the
    partial-unique-index ("at most one active game per guild").
    """
    seats = [
        Seat(
            seat_no=1,
            display_name="H1",
            discord_user_id="u1",
            is_llm=False,
            persona_key=None,
        ),
        Seat(
            seat_no=2,
            display_name="L2",
            discord_user_id=None,
            is_llm=True,
            persona_key=persona_key,
        ),
    ]
    game = _game_with_id(
        f"game-for-{role.value}-{persona_key}",
        guild_id=f"guild-{role.value}-{persona_key}",
    )
    await repo.create_game(game)
    for s in seats:
        await repo.insert_seat(game.id, s)
    await repo.set_player_role(game.id, 1, Role.VILLAGER)
    await repo.set_player_role(game.id, 2, role)

    decider = _CapturingDecider()
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)
    me = next(p for p in players if p.seat_no == 2)
    my_seat = next(s for s in seats if s.seat_no == 2)

    await adapter._ask(game, me, my_seat, players, seats, task_text=task_text)

    assert len(decider.captured) == 1
    system_prompt, _ = decider.captured[0]
    return system_prompt


async def test_ask_system_prompt_contains_game_rules_for_any_role(repo: SqliteRepo) -> None:
    """Every LLM seat, regardless of role, must receive the fixed 9-player
    rules block in its system prompt (role distribution, win conditions,
    candidate-token rule)."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER)
    assert "人狼2" in system_prompt
    assert "村人3" in system_prompt
    assert "生存人狼数が 0" in system_prompt
    assert "生存人狼数が生存非人狼人数以上" in system_prompt
    assert "候補トークン" in system_prompt


async def test_ask_system_prompt_contains_3_1_roller_rules_for_any_role(
    repo: SqliteRepo,
) -> None:
    """3-1 / seer roller / black-stop guidance must reach every seat via the
    shared rules block."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER)
    assert "3-1" in system_prompt
    assert "占いローラー" in system_prompt
    assert "黒ストップ" in system_prompt
    assert "真狼狼" in system_prompt


async def test_ask_system_prompt_contains_2_2_medium_roller_rules_for_any_role(
    repo: SqliteRepo,
) -> None:
    """2-2 / medium-roller guidance must reach every seat via the shared rules
    block."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MEDIUM)
    assert "2-2" in system_prompt
    assert "霊媒ローラー" in system_prompt
    assert "原則として完走" in system_prompt


async def test_ask_system_prompt_contains_2_1_and_1_2_formations_for_any_role(
    repo: SqliteRepo,
) -> None:
    """2-1 and 1-2 progression guidance must reach every seat via the shared
    rules block, alongside the existing 3-1 / 2-2 anchors."""
    for role in (
        Role.VILLAGER,
        Role.SEER,
        Role.MEDIUM,
        Role.KNIGHT,
        Role.WEREWOLF,
        Role.MADMAN,
    ):
        system_prompt = await _capture_ask_system_prompt(repo, role)
        assert "2-1" in system_prompt, f"{role.name} missed 2-1"
        assert "1-2" in system_prompt, f"{role.name} missed 1-2"


async def test_ask_system_prompt_contains_advanced_guard_vocab_for_any_role(
    repo: SqliteRepo,
) -> None:
    """Advanced guard vocabulary (鉄板護衛 / 捨て護衛 / 連続護衛不可 / 護衛読み /
    護衛誘導) lives in the shared rules block so every seat — not just the
    knight — can interpret these terms when they appear in the public log."""
    for role in (
        Role.VILLAGER,
        Role.SEER,
        Role.MEDIUM,
        Role.KNIGHT,
        Role.WEREWOLF,
        Role.MADMAN,
    ):
        system_prompt = await _capture_ask_system_prompt(repo, role)
        assert "鉄板護衛" in system_prompt, f"{role.name} missed 鉄板護衛"
        assert "捨て護衛" in system_prompt, f"{role.name} missed 捨て護衛"
        assert "連続護衛不可" in system_prompt, f"{role.name} missed 連続護衛不可"
        assert "護衛読み" in system_prompt, f"{role.name} missed 護衛読み"
        assert "護衛誘導" in system_prompt, f"{role.name} missed 護衛誘導"


async def test_ask_system_prompt_contains_enthusiast_checklist_for_any_role(
    repo: SqliteRepo,
) -> None:
    """Every seat must receive the 発言の根拠チェックリスト anchoring speeches
    in CO history / divination history / vote history / rope count and capping
    evidence to 1–2 points."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER)
    assert "CO 履歴" in system_prompt
    assert "判定履歴" in system_prompt
    assert "投票履歴" in system_prompt
    assert "縄数" in system_prompt
    assert "1〜2 点" in system_prompt


async def test_ask_system_prompt_contains_fake_co_legality_for_any_role(
    repo: SqliteRepo,
) -> None:
    """Fake-CO legality constraints live in common rules so both wolf and
    madman see them, and the wolf-coordination leak guards still hold when
    exercising the madman path."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MADMAN)
    assert "実ルール上あり得る内容" in system_prompt
    assert "自分護衛" in system_prompt
    assert "同一対象連続護衛" in system_prompt
    # Leak guards must still hold even with the new common-rules additions.
    # Bare 相方 (actor mode, partner-known) absent; 相方候補 (public-log
    # inference noun) is allowed in the shared 2-wolf-pair-inference rules.
    assert not re.search(r"相方(?!候補)", system_prompt)
    assert "襲撃先を揃える" not in system_prompt


async def test_ask_system_prompt_contains_day1_fake_seer_must_white_for_any_role(
    repo: SqliteRepo,
) -> None:
    """The NIGHT_0 day-1 fake-seer-must-white rule lives in the shared rules
    block so every seat sees it. Exercising via VILLAGER (no wolf-strategy
    leak) doubles as a non-leak guard."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER)
    assert "NIGHT_0" in system_prompt
    assert "初回" in system_prompt
    assert "day 1" in system_prompt
    assert "必ず白を主張" in system_prompt
    assert "day 1 で初回黒主張はしない" in system_prompt
    assert "偽占い師の黒結果主張は day 2 以降" in system_prompt
    # Wolf-coordination guard still holds for the villager seat. Bare 相方
    # (actor mode) absent; 相方候補 (inference) allowed.
    assert not re.search(r"相方(?!候補)", system_prompt)
    assert "襲撃先を揃える" not in system_prompt


async def test_ask_system_prompt_wolf_seat_includes_wolf_strategy(repo: SqliteRepo) -> None:
    """A werewolf LLM must receive wolf-coordination tips in its system
    prompt (`相方`, `襲撃先を揃える`)."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF)
    assert "相方" in system_prompt
    assert "襲撃先を揃える" in system_prompt


async def test_ask_system_prompt_non_wolf_excludes_wolf_strategy(repo: SqliteRepo) -> None:
    """A non-wolf LLM must NOT receive wolf-coordination tips. This guards
    against strategy leakage through `build_system_prompt`. Bare `相方`
    (actor-mode, partner-known) and `襲撃先を揃える` are wolf-only; the
    inference noun `相方候補` is allowed in the shared pair-inference rules
    and non-wolf strategies."""
    for role in (Role.SEER, Role.MEDIUM, Role.KNIGHT, Role.VILLAGER, Role.MADMAN):
        system_prompt = await _capture_ask_system_prompt(repo, role)
        assert not re.search(r"相方(?!候補)", system_prompt), (
            f"{role.name} saw bare '相方' (actor mode) in system prompt"
        )
        assert "襲撃先を揃える" not in system_prompt, (
            f"{role.name} saw '襲撃先を揃える' in system prompt"
        )


async def test_ask_system_prompt_madman_excludes_wolf_positions_assumption(
    repo: SqliteRepo,
) -> None:
    """The madman must be told explicitly NOT to assume real wolf positions,
    and must NOT receive wolf-coordination tips. The prohibition phrase must
    be present; the wolf playbook vocabulary must not. Bare `相方` (actor
    mode, partner-known) is forbidden; `相方候補` (public-log inference) is
    allowed since the madman reasons about who B is from public logs."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MADMAN)
    assert "人狼位置を知っている前提で話してはならない" in system_prompt
    assert not re.search(r"相方(?!候補)", system_prompt)
    assert "襲撃先を揃える" not in system_prompt


async def test_ask_system_prompt_wolf_seat_includes_attack_evaluation_axes(
    repo: SqliteRepo,
) -> None:
    """End-to-end: a werewolf LLM's system prompt carries the new 4-axis attack
    rubric (襲撃価値 / 護衛されやすさ / 騎士候補度) and the 騎士探し approach
    label via `_build_strategy_block(Role.WEREWOLF)`."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF)
    assert "襲撃価値" in system_prompt
    assert "護衛されやすさ" in system_prompt
    assert "騎士候補度" in system_prompt
    assert "騎士探し" in system_prompt


async def test_ask_system_prompt_wolf_attack_task_includes_checklist(
    repo: SqliteRepo,
) -> None:
    """When the wolf's task_text is the actual WOLF_ATTACK night-action prompt,
    the same 4-axis rubric must reach the LLM via the `{task_block}` slot — not
    only via the strategy block. Exercises the path `_one_night_action` uses."""
    task_text = task_night_action(SubmissionType.WOLF_ATTACK, ["席1 A", "席2 B"])
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF, task_text=task_text)
    assert "襲撃価値" in system_prompt
    assert "護衛されやすさ" in system_prompt
    assert "騎士候補度" in system_prompt
    assert "翌日の説明しやすさ" in system_prompt


async def test_ask_system_prompt_non_wolf_excludes_wolf_attack_vocabulary(
    repo: SqliteRepo,
) -> None:
    """Wolf-only attack-evaluation vocabulary must not bleed into any non-wolf
    seat's system prompt via the strategy block. Anchors mirror the unit-level
    leak guard but exercise the full prompt assembly."""
    for role in (Role.SEER, Role.MEDIUM, Role.KNIGHT, Role.VILLAGER, Role.MADMAN):
        system_prompt = await _capture_ask_system_prompt(repo, role)
        assert "騎士候補を噛む" not in system_prompt, (
            f"wolf-only '騎士候補を噛む' leaked into {role.name}"
        )
        assert "護衛リスクを読んで噛む" not in system_prompt, (
            f"wolf-only '護衛リスクを読んで噛む' leaked into {role.name}"
        )


async def test_ask_system_prompt_wolf_vote_task_includes_partner_checklist(
    repo: SqliteRepo,
) -> None:
    """When a wolf voter's task_text is the partner-aware vote task, the
    partner-name and vote-discipline checklist must reach the LLM via the
    `{task_block}` slot. Exercises the path `_one_vote` uses for wolf seats."""
    task_text = task_vote(
        ["席1 H1"],
        runoff=False,
        role=Role.WEREWOLF,
        wolf_partner_tokens=["席3 PartnerName"],
    )
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF, task_text=task_text)
    for token in (
        "仲間の人狼",
        "席3 PartnerName",
        "身内票",
        "ライン切り",
        "票筋",
        "透け",
    ):
        assert token in system_prompt, f"wolf vote system prompt missing {token!r}"


async def test_ask_system_prompt_wolf_runoff_vote_task_includes_runoff_checklist(
    repo: SqliteRepo,
) -> None:
    """The wolf runoff vote task injects the 決選投票 PP/RPP comparison into
    the system prompt on top of the base partner checklist."""
    task_text = task_vote(
        ["席1 H1"],
        runoff=True,
        role=Role.WEREWOLF,
        wolf_partner_tokens=["席3 PartnerName"],
    )
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF, task_text=task_text)
    for token in ("決選投票", "透け", "PP/RPP", "仲間の人狼"):
        assert token in system_prompt, f"wolf runoff vote system prompt missing {token!r}"


async def test_ask_system_prompt_non_wolf_vote_task_excludes_partner_vocabulary(
    repo: SqliteRepo,
) -> None:
    """Non-wolf voters get the base `task_vote` text. Partner-name and
    partner-action vocabulary must never appear in their system prompt,
    even though `身内票` / `ライン切り` themselves are shared rules vocab."""
    for role in (Role.SEER, Role.MEDIUM, Role.KNIGHT, Role.VILLAGER, Role.MADMAN):
        task_text = task_vote(["席1 H1"], runoff=False, role=role)
        system_prompt = await _capture_ask_system_prompt(repo, role, task_text=task_text)
        assert "仲間の人狼" not in system_prompt, (
            f"'仲間の人狼' leaked into {role.name} vote prompt"
        )
        assert "相方を救" not in system_prompt, f"'相方を救' leaked into {role.name} vote prompt"
        assert "相方を切" not in system_prompt, f"'相方を切' leaked into {role.name} vote prompt"


async def test_ask_system_prompt_madman_vote_task_drops_partner_token(
    repo: SqliteRepo,
) -> None:
    """If a caller passes a partner token for a madman voter, `task_vote`'s
    role gate must drop it before the prompt is built. The madman never
    learns real wolf positions through the vote task."""
    task_text = task_vote(
        ["席1 H1"],
        runoff=False,
        role=Role.MADMAN,
        wolf_partner_tokens=["席3 X"],
    )
    assert "仲間の人狼" not in task_text
    assert "席3 X" not in task_text
    system_prompt = await _capture_ask_system_prompt(repo, Role.MADMAN, task_text=task_text)
    assert "仲間の人狼" not in system_prompt
    assert "席3 X" not in system_prompt


async def test_ask_system_prompt_role_strategy_isolated_between_roles(
    repo: SqliteRepo,
) -> None:
    """A role's own strategy phrase appears in its system prompt; other roles'
    unique phrases must not. Integration-level analog of
    `test_strategy_block_no_cross_role_leak` in test_llm_prompt_builder.py."""
    unique_phrases = {
        Role.WEREWOLF: "相方を露骨に庇いすぎない",
        Role.MADMAN: "人狼位置を知っている前提で話してはならない",
        Role.SEER: "判定履歴を時系列で一貫",
        Role.MEDIUM: "処刑された相手が狂人でも",
        Role.KNIGHT: "前夜と違う相手を選ぶ",
        Role.VILLAGER: "CO 騙りは村陣営としては行わない",
    }
    for role, own_phrase in unique_phrases.items():
        system_prompt = await _capture_ask_system_prompt(repo, role)
        assert own_phrase in system_prompt, (
            f"{role.name} did not see its own phrase in system prompt"
        )
        for other_role, other_phrase in unique_phrases.items():
            if other_role is role:
                continue
            assert other_phrase not in system_prompt, (
                f"{other_role.name}'s tip leaked into {role.name}'s system prompt"
            )


async def test_ask_system_prompt_includes_speech_profile_section(repo: SqliteRepo) -> None:
    """Every LLM seat's system prompt carries the new `## 話法` section with
    the persona's first-person and the static common rule from the template."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER, persona_key="setsu")
    assert "## 話法" in system_prompt
    assert "『私』" in system_prompt
    assert "1 発話に入れてよい特徴語は多くても 1 個" in system_prompt


async def test_ask_system_prompt_speech_profile_per_seat(repo: SqliteRepo) -> None:
    """Different persona_keys produce different speech blocks via _ask —
    setsu's prompt carries『私』, yuriko's carries『この身』, and neither
    block cross-contaminates the other's first person."""
    setsu_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER, persona_key="setsu")
    yuriko_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER, persona_key="yuriko")
    assert "『私』" in setsu_prompt
    assert "『この身』" not in setsu_prompt
    assert "『この身』" in yuriko_prompt
    assert "『私』" not in yuriko_prompt.split("## 話法")[1]


async def test_ask_system_prompt_kukrushka_uses_silent_gesture(repo: SqliteRepo) -> None:
    """Kukrushka's prompt renders in silent_gesture mode — gesture examples
    replace the normal `一人称` line."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER, persona_key="kukrushka")
    assert "叙述モード" in system_prompt
    assert "所作" in system_prompt


async def test_ask_system_prompt_sq_flags_death_as_low_frequency(repo: SqliteRepo) -> None:
    """SQ's `DEATH` signature phrase must reach the LLM together with the
    low-frequency advisory — otherwise the LLM would pepper every utterance
    with it."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER, persona_key="sq")
    assert "DEATH" in system_prompt
    assert "低頻度" in system_prompt


async def test_ask_system_prompt_non_wolf_excludes_wolf_strategy_even_with_speech_block(
    repo: SqliteRepo,
) -> None:
    """The non-wolf isolation invariant must hold across all personas, not
    just the default. If a future `forbidden_overuse` or other speech-profile
    string ever contains wolf-coordination vocabulary (`相方` /
    `襲撃先を揃える`), this regression guard fires."""
    for role in (Role.SEER, Role.MEDIUM, Role.KNIGHT, Role.VILLAGER, Role.MADMAN):
        for pkey in ("setsu", "sq", "yuriko", "kukrushka"):
            system_prompt = await _capture_ask_system_prompt(repo, role, persona_key=pkey)
            assert not re.search(r"相方(?!候補)", system_prompt), (
                f"{role.name}/{pkey} saw bare '相方' (actor mode) in system prompt"
            )
            assert "襲撃先を揃える" not in system_prompt, (
                f"{role.name}/{pkey} saw '襲撃先を揃える' in system prompt"
            )


async def test_ask_system_prompt_contains_co_evaluation_rules_for_any_role(
    repo: SqliteRepo,
) -> None:
    """The shared CO evaluation guidance (single-CO default-truthy, conditions
    to suspect, counter-CO comparison axes) must reach every LLM seat via the
    rules block. `_build_game_rules_block` is role-independent so one role
    suffices to exercise the production path."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER)
    assert "単独 CO" in system_prompt
    assert "真の役職者にかなり近い" in system_prompt
    assert "対抗 CO" in system_prompt
    assert "噛み筋" in system_prompt


async def test_ask_system_prompt_distinguishes_sole_survivor_from_lone_co(
    repo: SqliteRepo,
) -> None:
    """Every LLM seat must receive the refinement that distinguishes a truly-
    lone CO (no counter ever appeared) from a sole-survivor CO (counter was
    executed or attacked). The rules block enforces: 2+ historical COs → do
    not auto-trust the survivor, and dead CO holders stay in the comparison
    via death-timing integrity."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER)
    assert "2 人以上" in system_prompt
    assert "自動的に真置きしない" in system_prompt
    assert "死亡済み" in system_prompt
    assert "死亡タイミング" in system_prompt


async def test_ask_system_prompt_warns_against_last_surviving_co_for_any_role(
    repo: SqliteRepo,
) -> None:
    """Every LLM seat must receive the conclusion-side refinement: a
    last-surviving CO is not automatically true. Wolves can leave an info role
    unattacked, get the counter executed, or keep a CO around for protective
    cover. Sample multiple roles to exercise both wolf-faction and village-
    faction paths through the shared rules block."""
    for role in (Role.VILLAGER, Role.SEER, Role.WEREWOLF, Role.MADMAN, Role.KNIGHT):
        system_prompt = await _capture_ask_system_prompt(repo, role)
        assert "最後まで生き残った" in system_prompt, f"{role.name} missed last-survivor warning"
        assert "噛まずに残した" in system_prompt, f"{role.name} missed wolf-skip framing"
        assert "単独 CO だから真" in system_prompt, f"{role.name} missed shortcut prohibition"


async def test_ask_system_prompt_villager_seat_prohibits_villager_co(
    repo: SqliteRepo,
) -> None:
    """A villager LLM's system prompt must explicitly forbid declaring
    '村人CO' / '素村CO' / '普通の村人です' / '役職は村人です' as a trust-buy and
    must point at the alternative '非 CO の灰' stance. The guidance reaches the
    villager via `_ROLE_STRATEGIES[Role.VILLAGER]`, so this test pins the
    end-to-end composition path through `build_system_prompt`."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER)
    assert "村人CO" in system_prompt
    assert "素村CO" in system_prompt
    assert "普通の村人です" in system_prompt
    assert "役職は村人です" in system_prompt
    assert "村人は能力結果を持たない" in system_prompt
    assert "非 CO の灰" in system_prompt


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN, Role.SEER, Role.MEDIUM, Role.KNIGHT])
async def test_ask_system_prompt_villager_co_prohibition_isolated(
    repo: SqliteRepo, role: Role
) -> None:
    """The villager-CO prohibition is scoped to the villager strategy block
    only; other roles must not see '村人CO' / '素村CO' in their system prompt.
    Cross-leak would either confuse fake-CO planning (wolf, madman) or
    accidentally suppress legitimate role-CO (seer, medium, knight)."""
    system_prompt = await _capture_ask_system_prompt(repo, role)
    assert "村人CO" not in system_prompt, f"{role.name} saw '村人CO' in system prompt"
    assert "素村CO" not in system_prompt, f"{role.name} saw '素村CO' in system prompt"


async def test_ask_system_prompt_explains_medium_white_semantics_for_any_role(
    repo: SqliteRepo,
) -> None:
    """Every LLM seat must receive the shared rule that medium-white means
    `not a real werewolf` only — not a role-claim refutation. Sampling one
    role suffices because `_build_game_rules_block` is role-independent."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER)
    assert "本物の人狼ではない" in system_prompt
    assert "真占い師だった可能性と矛盾しない" in system_prompt
    assert "偽扱いしない" in system_prompt


async def test_ask_system_prompt_contains_advanced_terminology_for_any_role(
    repo: SqliteRepo,
) -> None:
    """Every LLM seat (wolf, madman, villager, info role) must receive the
    advanced jinro terminology (グレラン / 縄計算 / スケール / 確白 / 確黒 /
    パンダ / PP / RPP) via the shared rules block. Sampling 4 representative
    roles exercises `build_system_prompt` for both wolf-faction and village-
    faction paths, since the terminology is emitted independent of role."""
    for role in (Role.VILLAGER, Role.SEER, Role.WEREWOLF, Role.MADMAN):
        system_prompt = await _capture_ask_system_prompt(repo, role)
        assert "グレラン" in system_prompt, f"{role.name} missed グレラン"
        assert "縄計算" in system_prompt, f"{role.name} missed 縄計算"
        assert "スケール" in system_prompt, f"{role.name} missed スケール"
        assert "確白" in system_prompt, f"{role.name} missed 確白"
        assert "確黒" in system_prompt, f"{role.name} missed 確黒"
        assert "パンダ" in system_prompt, f"{role.name} missed パンダ"
        assert "PP" in system_prompt, f"{role.name} missed PP"
        assert "RPP" in system_prompt, f"{role.name} missed RPP"


async def test_ask_system_prompt_medium_guards_against_seer_co_white_misread(
    repo: SqliteRepo,
) -> None:
    """The Medium LLM's system prompt must carry the role-specific guidance:
    medium-white on an executed Seer-CO is NOT proof of a fake seer; the
    Medium should separate real-seer from non-wolf-fake hypotheses."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MEDIUM)
    assert "占い師 CO 偽の証明ではない" in system_prompt
    assert "真占い師だった可能性" in system_prompt
    assert "狂人" in system_prompt


async def test_ask_system_prompt_knight_includes_protection_success_co_strategy(
    repo: SqliteRepo,
) -> None:
    """The knight LLM's system prompt must cover the peaceful-morning /
    protection-success CO pathway with the guard target disclosure rule."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.KNIGHT)
    assert "平和な朝" in system_prompt
    assert "護衛成功" in system_prompt
    assert "護衛先を添えて" in system_prompt


async def test_ask_system_prompt_seer_includes_counter_co_strategy(
    repo: SqliteRepo,
) -> None:
    """A true seer LLM must receive proactive-CO (when no seer CO has appeared),
    counter-CO (when a fake seer appears), and black-pull CO guidance so the
    true seer doesn't stay silent and cede single-truth treatment to a fake."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.SEER)
    assert "まだ占い師 CO が出ていない" in system_prompt
    assert "対抗 CO" in system_prompt
    assert "時系列で公開" in system_prompt
    assert "黒を引いた場合" in system_prompt


async def test_ask_system_prompt_medium_includes_counter_co_strategy(
    repo: SqliteRepo,
) -> None:
    """A true medium LLM must receive the post-execution result-publication
    duty and the counter-CO pathway, with explicit self-roller vulnerability
    framing so the medium doesn't stay silent against a fake medium."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MEDIUM)
    assert "処刑が発生した翌日" in system_prompt
    assert "対抗霊媒" in system_prompt
    assert "ローラー" in system_prompt


async def test_ask_system_prompt_knight_includes_legal_guard_history_and_endgame_co(
    repo: SqliteRepo,
) -> None:
    """A knight LLM's system prompt must cover endgame / about-to-be-hung CO
    timing AND must constrain the guard-diary to the bot's legal guard rules
    (no self-guard, no consecutive guard of the same seat, no dead-seat guard)."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.KNIGHT)
    assert "終盤" in system_prompt
    assert "吊られそう" in system_prompt
    assert "護衛履歴を日付順" in system_prompt
    assert "自分護衛" in system_prompt
    assert "同じ相手の連続護衛" in system_prompt


async def test_ask_system_prompt_knight_includes_advanced_guard_strategy(
    repo: SqliteRepo,
) -> None:
    """A knight LLM's system prompt must carry the advanced multi-axis guard
    reasoning: 鉄板護衛 / 捨て護衛 distinction, 連続護衛不可 awareness, and
    next-night planning. Existing knight unique anchor remains as a regression
    guard, and wolf-coordination leak guards still hold."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.KNIGHT)
    assert "鉄板護衛" in system_prompt
    assert "捨て護衛" in system_prompt
    assert "連続護衛不可" in system_prompt
    assert "次夜" in system_prompt
    # Knight unique anchor must remain (regression guard).
    assert "前夜と違う相手を選ぶ" in system_prompt
    # Wolf-coordination leak guards must still hold. Bare 相方 (actor mode)
    # absent; 相方候補 (public-log inference) allowed in knight pair-inference.
    assert not re.search(r"相方(?!候補)", system_prompt)
    assert "襲撃先を揃える" not in system_prompt


async def test_ask_system_prompt_knight_guard_task_includes_checklist(
    repo: SqliteRepo,
) -> None:
    """When the knight's task_text is the actual KNIGHT_GUARD night-action
    prompt, the 5-axis knight checklist (鉄板護衛 / 捨て護衛 / 1 名 selection)
    must reach the LLM via the `{task_block}` slot. None of the wolf-task
    forbidden literal substrings may appear — that would trip the existing
    parametrized leak test in the unit-level prompt builder tests."""
    task_text = task_night_action(SubmissionType.KNIGHT_GUARD, ["席1 A", "席2 B"])
    system_prompt = await _capture_ask_system_prompt(repo, Role.KNIGHT, task_text=task_text)
    # Positive — task-level checklist reached the LLM.
    assert "鉄板護衛" in system_prompt
    assert "捨て護衛" in system_prompt
    assert "1 名" in system_prompt
    # Negative — wolf-task forbidden substrings absent from the task block.
    # (Note: 襲撃価値 / 護衛されやすさ / 騎士候補度 / 翌日の説明しやすさ /
    # 騎士探し still appear elsewhere in the wolf strategy block of a wolf
    # seat, but never in the knight seat's task_text.) The knight does not see
    # the wolf strategy block, so these literals must be absent from the full
    # knight system prompt as well.
    assert "襲撃価値" not in system_prompt
    assert "護衛されやすさ" not in system_prompt
    assert "騎士候補度" not in system_prompt
    assert "翌日の説明しやすさ" not in system_prompt
    assert "騎士探し" not in system_prompt


async def test_ask_system_prompt_wolf_seat_includes_fake_strategy(repo: SqliteRepo) -> None:
    """The werewolf LLM's system prompt must carry the fake-CO playbook:
    day-1 seer fake is offered as a *conditional* option (not unconditional),
    day-2+ medium/knight fake, the over-fake warning, and medium-roller /
    knight-legal-guard-history caveats."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF)
    assert "day 1" in system_prompt
    assert "占い師騙り" in system_prompt
    assert "霊媒師騙り" in system_prompt
    assert "騎士騙り" in system_prompt
    assert "6 人以上" in system_prompt
    assert "騙りすぎ" in system_prompt
    # Conditional framing — day-1 seer fake is no longer unconditional.
    assert "無条件" in system_prompt
    assert "潜伏" in system_prompt
    assert "相方が危険位置" in system_prompt
    # Day-1 first-result-white anchor + day-1 black prohibition + day-2+ deferral.
    assert "NIGHT_0 ランダム白" in system_prompt
    assert "必ず白を主張" in system_prompt
    assert "初日に黒を出す主張" in system_prompt
    assert "黒出しは day 2 以降" in system_prompt
    assert "前夜に占ったという想定" in system_prompt


async def test_ask_system_prompt_madman_includes_fake_strategy_without_wolf_coordination(
    repo: SqliteRepo,
) -> None:
    """The madman LLM's system prompt must carry the fake-CO playbook
    (day-1 seer fake, day-2+ medium/knight fake, over-fake warning) as a
    *conditional* option with misfire caveats — and no wolf-coordination
    vocabulary (`相方` / `襲撃先を揃える`)."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MADMAN)
    assert "day 1" in system_prompt
    assert "占い師騙り" in system_prompt
    assert "霊媒師騙り" in system_prompt
    assert "騎士騙り" in system_prompt
    assert "6 人以上" in system_prompt
    assert "騙りすぎ" in system_prompt
    # Wolf-coordination vocabulary must not appear for the madman. Bare 相方
    # (actor mode, partner-known) absent; 相方候補 (public-log inference) allowed.
    assert not re.search(r"相方(?!候補)", system_prompt)
    assert "襲撃先を揃える" not in system_prompt
    # Existing prohibition phrase must also still be present.
    assert "人狼位置を知っている前提で話してはならない" in system_prompt
    # Conditional framing + misfire / white-out caveats.
    assert "無条件ではなく" in system_prompt
    assert "複数の占い師 CO" in system_prompt
    assert "誤爆リスク" in system_prompt
    assert "白先が本物の狼とは限らない" in system_prompt
    # Day-1 first-result-white anchor + day-1 black prohibition + day-2+ deferral.
    assert "NIGHT_0 ランダム白" in system_prompt
    assert "必ず白を主張" in system_prompt
    assert "初日に黒を出す主張" in system_prompt
    assert "黒出しは day 2 以降" in system_prompt
    assert "誤爆リスクは day 2 以降の黒出しでも常に残る" in system_prompt


async def test_ask_system_prompt_seer_includes_night_targeting_axes(
    repo: SqliteRepo,
) -> None:
    """The true seer's system prompt must carry the night-divination targeting
    axes (占い価値 / 灰を狭める / 対抗 CO / 囲い候補 / 投票 /
    白でも黒でも情報が落ちる) via `_ROLE_STRATEGIES[Role.SEER]`. End-to-end
    analog of the unit-level `test_seer_strategy_includes_night_divination_targeting_axes`."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.SEER)
    assert "占い価値" in system_prompt
    assert "灰を狭める" in system_prompt
    assert "対抗 CO" in system_prompt
    assert "囲い候補" in system_prompt
    assert "投票" in system_prompt
    assert "白でも黒でも情報が落ちる" in system_prompt


async def test_ask_system_prompt_seer_divine_task_includes_targeting_checklist(
    repo: SqliteRepo,
) -> None:
    """When the seer's task_text is the actual SEER_DIVINE night-action prompt,
    the targeting axes must reach the LLM via the `{task_block}` slot — the
    parallel of the wolf-attack and knight-guard task tests above. Wolf-task
    forbidden substrings must remain absent (existing leak guard reinforced)."""
    task_text = task_night_action(SubmissionType.SEER_DIVINE, ["席1 A", "席2 B"])
    system_prompt = await _capture_ask_system_prompt(repo, Role.SEER, task_text=task_text)
    # Positive — task-level targeting checklist reached the LLM.
    assert "占い価値" in system_prompt
    assert "灰を狭める" in system_prompt
    assert "対抗 CO" in system_prompt
    assert "囲い候補" in system_prompt
    assert "投票" in system_prompt
    assert "白でも黒でも情報が落ちる" in system_prompt
    # Negative — wolf-task forbidden substrings absent from the seer's task.
    assert "襲撃価値" not in task_text
    assert "護衛されやすさ" not in task_text
    assert "騎士候補度" not in task_text
    assert "翌日の説明しやすさ" not in task_text
    assert "騎士探し" not in task_text


async def test_ask_system_prompt_wolf_seat_includes_day2_round1_fake_publication(
    repo: SqliteRepo,
) -> None:
    """The werewolf LLM's system prompt must carry the day-2+ round-1 fake-
    result publication imperative for seer/medium/knight fakes, with the
    integration cues (相方 / 囲い / 噛み筋 / 霊媒結果 / 合法な護衛履歴)."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF)
    assert "day 2 以降" in system_prompt
    assert "1 巡目" in system_prompt
    assert "前夜" in system_prompt
    assert "能力結果" in system_prompt
    assert "合法な護衛履歴" in system_prompt
    # Wolf-private integration cues must be present.
    assert "相方" in system_prompt
    assert "囲い" in system_prompt
    assert "噛み筋" in system_prompt
    assert "霊媒結果" in system_prompt


async def test_ask_system_prompt_madman_includes_day2_round1_fake_publication(
    repo: SqliteRepo,
) -> None:
    """The madman LLM's system prompt must carry the day-2+ round-1 fake-
    result publication imperative with explicit misfire framing — and must
    NOT carry wolf-coordination vocabulary."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MADMAN)
    assert "day 2 以降" in system_prompt
    assert "1 巡目" in system_prompt
    assert "前夜" in system_prompt
    assert "能力結果" in system_prompt
    assert "誤爆リスク" in system_prompt
    assert "白先が本物の狼とは限らない" in system_prompt
    assert "処刑なしの日は結果なし" in system_prompt
    assert "合法な護衛履歴" in system_prompt
    # Wolf-coordination vocabulary must remain absent. Bare 相方 (actor mode)
    # forbidden; 相方候補 (public-log inference) allowed in pair-inference.
    assert not re.search(r"相方(?!候補)", system_prompt)
    assert "襲撃先を揃える" not in system_prompt


async def test_discussion_speech_day2_round1_passes_first_round_rule(
    repo: SqliteRepo,
) -> None:
    """End-to-end: when `_run_discussion_rounds` calls
    `_do_one_discussion_speech(discussion_round=1)` on a day-2 game, the
    captured task block must contain the day-2+ round-1 mandatory rule. This
    test drives the per-seat path directly so the round threading is asserted
    independently of the round-loop bookkeeping."""
    game = Game(
        id="g-d2r1",
        guild_id="gu-d2r1",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=2,
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
            display_name="セツ",
            discord_user_id=None,
            is_llm=True,
            persona_key="setsu",
        ),
    ]
    for s in seats:
        await repo.insert_seat(game.id, s)
    await repo.set_player_role(game.id, 1, Role.VILLAGER)
    await repo.set_player_role(game.id, 2, Role.SEER)

    decider = _CapturingDecider()
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)
    llm_player = next(p for p in players if p.seat_no == 2)
    llm_seat = next(s for s in seats if s.seat_no == 2)

    await adapter._do_one_discussion_speech(
        game=game, player=llm_player, seat=llm_seat, seats=seats, discussion_round=1
    )

    assert len(decider.captured) == 1
    system_prompt, _ = decider.captured[0]
    assert "1 巡目" in system_prompt
    assert "前夜の能力結果" in system_prompt


async def test_discussion_speech_day2_round2_omits_first_round_rule(
    repo: SqliteRepo,
) -> None:
    """End-to-end: round 2 of a day-2 game must NOT carry the
    'must attach prior-night results' imperative."""
    game = Game(
        id="g-d2r2",
        guild_id="gu-d2r2",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=2,
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
            display_name="セツ",
            discord_user_id=None,
            is_llm=True,
            persona_key="setsu",
        ),
    ]
    for s in seats:
        await repo.insert_seat(game.id, s)
    await repo.set_player_role(game.id, 1, Role.VILLAGER)
    await repo.set_player_role(game.id, 2, Role.SEER)

    decider = _CapturingDecider()
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)
    llm_player = next(p for p in players if p.seat_no == 2)
    llm_seat = next(s for s in seats if s.seat_no == 2)

    await adapter._do_one_discussion_speech(
        game=game, player=llm_player, seat=llm_seat, seats=seats, discussion_round=2
    )

    assert len(decider.captured) == 1
    system_prompt, _ = decider.captured[0]
    # The day-2+ first-round imperative must NOT appear in round 2.
    assert "前夜の能力結果" not in system_prompt
    # The base intent/skip contract must still be present.
    assert "intent=speak" in system_prompt
    assert "intent=skip" in system_prompt


async def test_discussion_speech_day1_round1_omits_day2_rule(
    repo: SqliteRepo,
) -> None:
    """End-to-end: day 1 round 1 must NOT carry the day-2+ rule. Day-1 first
    results are constrained by the existing NIGHT_0 / day-1-white rules in the
    game-rules block, not by the new day-2+ round-1 rule."""
    game = Game(
        id="g-d1r1",
        guild_id="gu-d1r1",
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
            display_name="セツ",
            discord_user_id=None,
            is_llm=True,
            persona_key="setsu",
        ),
    ]
    for s in seats:
        await repo.insert_seat(game.id, s)
    await repo.set_player_role(game.id, 1, Role.VILLAGER)
    await repo.set_player_role(game.id, 2, Role.SEER)

    decider = _CapturingDecider()
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)
    llm_player = next(p for p in players if p.seat_no == 2)
    llm_seat = next(s for s in seats if s.seat_no == 2)

    await adapter._do_one_discussion_speech(
        game=game, player=llm_player, seat=llm_seat, seats=seats, discussion_round=1
    )

    assert len(decider.captured) == 1
    system_prompt, _ = decider.captured[0]
    # Day-1 round-1 must omit the day-2+ rule. The literal day-2+ imperative
    # phrase ("これは day 2 以降の 1 巡目発言です。") must not appear.
    assert "day 2 以降の 1 巡目発言" not in system_prompt
    assert "前夜の能力結果" not in system_prompt


def test_task_daytime_speech_runoff_default_omits_first_round_rule() -> None:
    """`_do_one_runoff_speech` calls `task_daytime_speech(game.day_number)`
    with no `discussion_round`. The default branch must NOT include the
    day-2+ first-round mandatory result rule, even on day 2+."""
    text = task_daytime_speech(2)
    assert "1 巡目" not in text
    assert "前夜の能力結果" not in text


# ---------------------------------- user context analysis blocks via _ask
async def _capture_ask_user_context(
    repo: SqliteRepo,
    role: Role,
    *,
    game_id: str,
    guild_id: str,
    extra_seats: list[Seat] | None = None,
    extra_roles: list[tuple[int, Role]] | None = None,
    speeches: list[tuple[int, str]] | None = None,
) -> str:
    """Seed a small game, optionally insert PLAYER_SPEECH logs, run `_ask`,
    and return the captured user_context. Mirrors `_capture_ask_system_prompt`.

    `extra_seats` plus `extra_roles` lets a caller add seats beyond the
    default 2-seat (Villager + role-under-test) layout. `speeches` is a list
    of (actor_seat, text) pairs inserted as PLAYER_SPEECH public logs.
    """
    seats = [
        Seat(
            seat_no=1,
            display_name="H1",
            discord_user_id="u1",
            is_llm=False,
            persona_key=None,
        ),
        Seat(
            seat_no=2,
            display_name="L2",
            discord_user_id=None,
            is_llm=True,
            persona_key="setsu",
        ),
    ]
    if extra_seats:
        seats.extend(extra_seats)
    game = _game_with_id(game_id, guild_id=guild_id)
    await repo.create_game(game)
    for s in seats:
        await repo.insert_seat(game.id, s)
    await repo.set_player_role(game.id, 1, Role.VILLAGER)
    await repo.set_player_role(game.id, 2, role)
    for seat_no, extra_role in extra_roles or []:
        await repo.set_player_role(game.id, seat_no, extra_role)
    for seat_no, text in speeches or []:
        await repo.insert_log_public(
            LogEntry(
                game_id=game.id,
                day=1,
                phase=Phase.DAY_DISCUSSION,
                kind="PLAYER_SPEECH",
                actor_seat=seat_no,
                visibility="PUBLIC",
                text=text,
                created_at=1,
            )
        )

    decider = _CapturingDecider()
    adapter = LLMAdapter(repo=repo, decider=decider, rng=random.Random(0))
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)
    me = next(p for p in players if p.seat_no == 2)
    my_seat = next(s for s in seats if s.seat_no == 2)

    await adapter._ask(game, me, my_seat, players, seats, task_text="test-task")

    assert len(decider.captured) == 1
    _, user_context = decider.captured[0]
    return user_context


async def test_ask_user_context_contains_rope_block(repo: SqliteRepo) -> None:
    """The deterministic rope block survives in user_context. The CO parser
    sections (CO list / 盤面分類 / 役職推定メモ) are gone — the LLM now reads
    raw 公開ログ from `## 公開ログ要約` and judges in context."""
    user_context = await _capture_ask_user_context(
        repo,
        Role.VILLAGER,
        game_id="game-analysis",
        guild_id="guild-analysis",
    )
    assert "## 縄数・PP/RPPリスク" in user_context
    assert "## CO・判定の機械整理" not in user_context
    assert "## 盤面分類" not in user_context
    assert "## 役職推定メモ (公開情報ベース)" not in user_context


async def test_ask_user_context_passes_raw_co_speech_through(repo: SqliteRepo) -> None:
    """A seat that uttered `占い師CO` in a public PLAYER_SPEECH log must reach
    the LLM as the seat-attributed raw line in `## 公開ログ要約`. No
    parser-style digest (`占い師CO: 席1`, `公開CO履歴ベース`) is rendered."""
    user_context = await _capture_ask_user_context(
        repo,
        Role.VILLAGER,
        game_id="game-co-extract",
        guild_id="guild-co-extract",
        speeches=[(1, "占い師COします。"), (2, "霊媒師COします。")],
    )
    assert "[PLAYER_SPEECH] 席1 H1: 占い師COします。" in user_context
    assert "[PLAYER_SPEECH] 席2 L2: 霊媒師COします。" in user_context
    # Parser-style digest output must NOT be present.
    assert "占い師CO: 席1" not in user_context
    assert "霊媒師CO: 席2" not in user_context
    assert "公開CO履歴ベース" not in user_context


async def test_ask_user_context_raw_logs_scoped_to_current_game_id(repo: SqliteRepo) -> None:
    """Raw PLAYER_SPEECH text from a different game must not leak into game B's
    user_context. Companion to `test_ask_scopes_logs_to_current_game_id`: that
    test verifies the raw log dump isolation; this version pins that the
    leaked CO speech and seat name from game A do not surface anywhere in
    game B's prompt now that the parser-derived sections are gone."""
    leak_seat = Seat(
        seat_no=1, display_name="LEAK", discord_user_id="ux", is_llm=False, persona_key=None
    )
    game_a = _game_with_id("g-leak-a", guild_id="guild-leak-a")
    await repo.create_game(game_a)
    await repo.insert_seat(game_a.id, leak_seat)
    await repo.insert_seat(
        game_a.id,
        Seat(
            seat_no=2,
            display_name="LEAK2",
            discord_user_id=None,
            is_llm=True,
            persona_key="setsu",
        ),
    )
    await repo.set_player_role(game_a.id, 1, Role.VILLAGER)
    await repo.set_player_role(game_a.id, 2, Role.SEER)
    # Game A: a CO speech that should NOT bleed into game B's user_context.
    await repo.insert_log_public(
        LogEntry(
            game_id=game_a.id,
            day=1,
            phase=Phase.DAY_DISCUSSION,
            kind="PLAYER_SPEECH",
            actor_seat=1,
            visibility="PUBLIC",
            text="占い師COします。",
            created_at=1,
        )
    )
    await repo.end_game(game_a.id, ended_at_epoch=2)

    user_context = await _capture_ask_user_context(
        repo,
        Role.VILLAGER,
        game_id="g-leak-b",
        guild_id="guild-leak-b",
    )
    # The leak seat's display_name from game A must not appear anywhere.
    assert "LEAK" not in user_context
    # Game B never logged a CO, so neither the raw speech nor any CO substring
    # from game A's logs should be present.
    assert "占い師COします" not in user_context
    assert "占い師CO" not in user_context


async def test_ask_user_context_no_wolf_partner_block_for_villager(repo: SqliteRepo) -> None:
    """Even after the analysis blocks were inserted between the wolf-partner
    block and the rest, non-wolves must never see the wolf-partner heading."""
    user_context = await _capture_ask_user_context(
        repo,
        Role.VILLAGER,
        game_id="g-no-wolf",
        guild_id="guild-no-wolf",
    )
    assert "## 仲間の人狼" not in user_context


# ----------------------------------- 2 人狼ペア推理 (wolf-pair inference)
# Service-level analogs of the pair-inference unit tests in
# test_llm_prompt_builder.py. Verify the new content actually reaches the LLM
# via the full system-prompt assembly path.


@pytest.mark.parametrize("role", list(Role))
async def test_ask_system_prompt_pair_inference_reaches_any_role(
    repo: SqliteRepo, role: Role
) -> None:
    """Every LLM seat must receive the shared 2-wolf-pair inference vocabulary
    via the rules block. `相方候補` (inference noun) and `2 人狼` (the canonical
    pair label) are the two shared anchors."""
    system_prompt = await _capture_ask_system_prompt(repo, role)
    assert "相方候補" in system_prompt, f"{role.name} missing pair-inference anchor '相方候補'"
    assert "2 人狼" in system_prompt, f"{role.name} missing pair-inference anchor '2 人狼'"


async def test_ask_system_prompt_wolf_seat_includes_two_wolf_set_framing(
    repo: SqliteRepo,
) -> None:
    """The werewolf system prompt must carry the `2 人狼セット` framing — every
    save/cut/cover/black-out/attack must be defensible as the same A-B line on
    later days — plus the `視点漏れ` prohibition (don't leak partner-known info
    in public speech)."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF)
    assert "2 人狼セット" in system_prompt
    assert "視点漏れ" in system_prompt


async def test_ask_system_prompt_madman_pair_inference_carries_misfire(
    repo: SqliteRepo,
) -> None:
    """The madman system prompt must carry the public-log pair-inference
    framing with explicit misfire awareness — without leaking wolf-coordination
    vocabulary. Bare `相方` (actor mode) must remain absent; `相方候補` is the
    only allowed form. `襲撃先を揃える` and `仲間の人狼` must be absent."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MADMAN)
    assert "相方候補" in system_prompt
    assert "公開ログからの推定" in system_prompt
    assert "誤爆" in system_prompt
    # Wolf-coordination vocab forbidden for the madman.
    assert not re.search(r"相方(?!候補)", system_prompt)
    assert "襲撃先を揃える" not in system_prompt
    assert "仲間の人狼" not in system_prompt


async def test_ask_system_prompt_wolf_vote_task_extends_with_two_wolf_set(
    repo: SqliteRepo,
) -> None:
    """When the wolf voter's task_text is the wolf-only vote task, the
    system prompt must contain the new `2 人狼セット` framing on top of the
    existing partner checklist anchors (`相方` / `身内票` / `ライン切り`)."""
    task_text = task_vote(
        ["席1 H1"],
        runoff=False,
        role=Role.WEREWOLF,
        wolf_partner_tokens=["席3 PartnerName"],
    )
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF, task_text=task_text)
    assert "2 人狼セット" in system_prompt
    # Existing wolf-only vote anchors preserved.
    assert "相方" in system_prompt
    assert "身内票" in system_prompt
    assert "ライン切り" in system_prompt


# ---------------------------------------------------------- provider deciders
#
# These tests exercise XAILLMActionDecider / DeepSeekLLMActionDecider /
# GeminiLLMActionDecider directly against fake clients to verify the exact
# kwargs sent at the SDK boundary that distinguishes each provider. The rest
# of this file uses Protocol-level fake deciders, but kwargs assertions need
# a fake of each SDK's call surface.


class _FakeChatCompletions:
    """Records create() kwargs and returns a canned chat-completion response."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        from types import SimpleNamespace

        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


class _FakeChat:
    def __init__(self, content: str) -> None:
        self.completions = _FakeChatCompletions(content)


class _FakeAsyncOpenAI:
    """Minimal stand-in for `openai.AsyncOpenAI` — exposes `.chat.completions.create`."""

    def __init__(self, content: str) -> None:
        self.chat = _FakeChat(content)


class _FakeGenAIModels:
    """Records generate_content() kwargs and returns a canned `.text` payload."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict[str, object]] = []

    async def generate_content(self, **kwargs: object) -> object:
        from types import SimpleNamespace

        self.calls.append(kwargs)
        return SimpleNamespace(text=self._content)


class _FakeGenAIAio:
    def __init__(self, content: str) -> None:
        self.models = _FakeGenAIModels(content)


class _FakeGenAIClient:
    """Minimal stand-in for `google.genai.Client` — exposes `.aio.models.generate_content`."""

    def __init__(self, content: str) -> None:
        self.aio = _FakeGenAIAio(content)


def _canned_action_json() -> str:
    return (
        '{"intent": "speak", "public_message": "test",'
        ' "target_name": null, "reason_summary": "ok", "confidence": 0.5}'
    )


async def test_xai_decider_sends_json_schema_response_format_no_reasoning_effort() -> None:
    """The xAI path uses json_schema strict mode and must NOT send DeepSeek-only
    knobs (`reasoning_effort`, `extra_body`) — Grok rejects them."""
    from wolfbot.services.llm_service import RESPONSE_SCHEMA, XAILLMActionDecider

    fake = _FakeAsyncOpenAI(_canned_action_json())
    decider = XAILLMActionDecider(client=fake, model="grok-x", timeout=12.0)  # type: ignore[arg-type]
    action = await decider.decide("sys", "ctx")

    assert action.intent == "speak"
    call = fake.chat.completions.calls[0]
    assert call["model"] == "grok-x"
    assert call["timeout"] == 12.0
    assert call["response_format"] == {"type": "json_schema", "json_schema": RESPONSE_SCHEMA}
    assert "reasoning_effort" not in call
    assert "extra_body" not in call
    assert call["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "ctx"},
    ]


async def test_deepseek_decider_sends_json_object_thinking_and_reasoning_effort() -> None:
    """DeepSeek path: json_object response_format, thinking enabled in extra_body,
    reasoning_effort forwarded, JSON contract appended to system prompt, and
    sampling controls deliberately omitted."""
    from wolfbot.services.llm_service import DeepSeekLLMActionDecider

    fake = _FakeAsyncOpenAI(_canned_action_json())
    decider = DeepSeekLLMActionDecider(
        client=fake,  # type: ignore[arg-type]
        model="deepseek-v4-flash",
        thinking="enabled",
        reasoning_effort="max",
        timeout=20.0,
    )
    action = await decider.decide("sys", "ctx")

    assert action.intent == "speak"
    call = fake.chat.completions.calls[0]
    assert call["model"] == "deepseek-v4-flash"
    assert call["timeout"] == 20.0
    assert call["response_format"] == {"type": "json_object"}
    assert call["extra_body"] == {"thinking": {"type": "enabled"}}
    assert call["reasoning_effort"] == "max"
    for forbidden in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        assert forbidden not in call
    messages = call["messages"]
    assert isinstance(messages, list)
    sys_msg = messages[0]["content"]
    assert isinstance(sys_msg, str)
    assert "sys" in sys_msg
    assert "json" in sys_msg.lower()
    assert messages[1] == {"role": "user", "content": "ctx"}


async def test_deepseek_decider_omits_reasoning_effort_when_thinking_disabled() -> None:
    """`thinking=disabled` should still send extra_body for symmetry but must
    NOT send reasoning_effort (semantically meaningless without thinking)."""
    from wolfbot.services.llm_service import DeepSeekLLMActionDecider

    fake = _FakeAsyncOpenAI(_canned_action_json())
    decider = DeepSeekLLMActionDecider(
        client=fake,  # type: ignore[arg-type]
        model="deepseek-v4-flash",
        thinking="disabled",
        reasoning_effort="high",
    )
    await decider.decide("sys", "ctx")

    call = fake.chat.completions.calls[0]
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in call


async def test_gemini_decider_sends_response_json_schema_and_thinking_level() -> None:
    """Gemini path: `response_mime_type=application/json` + `response_json_schema`,
    `thinking_level` forwarded via `ThinkingConfig`, system prompt as
    `system_instruction`, user context as `contents`. Sampling controls and
    DeepSeek-only knobs (`extra_body`, `reasoning_effort`) must NOT be sent."""
    from wolfbot.services.llm_service import RESPONSE_SCHEMA, GeminiLLMActionDecider

    fake = _FakeGenAIClient(_canned_action_json())
    decider = GeminiLLMActionDecider(
        client=fake,
        model="gemini-3-flash-preview",
        thinking_level="low",
        timeout=15.0,
    )
    action = await decider.decide("sys", "ctx")

    assert action.intent == "speak"
    call = fake.aio.models.calls[0]
    assert call["model"] == "gemini-3-flash-preview"
    assert call["contents"] == "ctx"
    config = call["config"]
    assert config.system_instruction == "sys"
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == RESPONSE_SCHEMA["schema"]
    # SDK normalizes the string "low" into `ThinkingLevel.LOW` (value="LOW"),
    # so compare on the case-insensitive value.
    assert config.thinking_config.thinking_level.value.lower() == "low"
    for forbidden in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        assert forbidden not in call
    assert "extra_body" not in call
    assert "reasoning_effort" not in call


def test_make_llm_decider_branches_on_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider-aware factory must wire the right decider class for each
    LLM_PROVIDER value. Construction goes through Settings so the validator
    runs end-to-end. The Gemini branch stubs `google.genai.Client` so the
    test never depends on a live ADC environment."""
    from pydantic import SecretStr

    from wolfbot.config import Settings
    from wolfbot.services.llm_service import (
        DeepSeekLLMActionDecider,
        GeminiLLMActionDecider,
        XAILLMActionDecider,
        make_llm_decider,
    )

    class _StubClient:
        def __init__(self, **kwargs: object) -> None:
            pass

    import google.genai

    monkeypatch.setattr(google.genai, "Client", _StubClient)

    base_kwargs: dict[str, object] = {
        "DISCORD_TOKEN": SecretStr("t"),
        "DISCORD_GUILD_ID": 1,
        "MAIN_TEXT_CHANNEL_ID": 2,
        "MAIN_VOICE_CHANNEL_ID": 3,
    }
    s_xai = Settings(  # type: ignore[arg-type]
        _env_file=None,
        **base_kwargs,
        LLM_PROVIDER="xai",
        XAI_API_KEY=SecretStr("x"),
    )
    assert isinstance(make_llm_decider(s_xai), XAILLMActionDecider)

    s_ds = Settings(  # type: ignore[arg-type]
        _env_file=None,
        **base_kwargs,
        LLM_PROVIDER="deepseek",
        DEEPSEEK_API_KEY=SecretStr("d"),
    )
    assert isinstance(make_llm_decider(s_ds), DeepSeekLLMActionDecider)

    s_gem = Settings(  # type: ignore[arg-type]
        _env_file=None,
        **base_kwargs,
        LLM_PROVIDER="gemini",
        GEMINI_VERTEX_PROJECT="my-project",
    )
    assert isinstance(make_llm_decider(s_gem), GeminiLLMActionDecider)


def test_make_gemini_decider_constructs_vertex_ai_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vertex AI client construction must pass `vertexai=True`, project,
    location, and `http_options`, and must NOT pass `api_key` (the SDK
    rejects api_key + vertexai together). Also verifies the decider
    captures model/thinking_level/timeout for downstream use."""
    captured: dict[str, object] = {}

    class _StubClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    import google.genai

    monkeypatch.setattr(google.genai, "Client", _StubClient)

    from wolfbot.services.llm_service import make_gemini_decider

    decider = make_gemini_decider(
        project="my-project",
        location="global",
        model="gemini-3-flash-preview",
        thinking_level="high",
        timeout=15.0,
    )

    assert captured["vertexai"] is True
    assert captured["project"] == "my-project"
    assert captured["location"] == "global"
    assert "api_key" not in captured
    http_options = captured["http_options"]
    # types.HttpOptions stores timeout in milliseconds.
    assert getattr(http_options, "timeout", None) == 15000
    assert decider.model == "gemini-3-flash-preview"
    assert decider.thinking_level == "high"
    assert decider.timeout == 15.0
