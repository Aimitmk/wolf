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


async def test_ask_system_prompt_contains_three_seer_co_elimination_for_any_role(
    repo: SqliteRepo,
) -> None:
    """3占いCO・2非狼確定で残る 1 人を確定黒級とする消去法は共通ルール経由で
    全 role の system prompt に届く。非狼確定の厳格さと前提崩壊時の解除も同様。"""
    for role in (
        Role.VILLAGER,
        Role.SEER,
        Role.MEDIUM,
        Role.KNIGHT,
        Role.WEREWOLF,
        Role.MADMAN,
    ):
        system_prompt = await _capture_ask_system_prompt(repo, role)
        assert "残る 1 人の占い師 CO を固定配役上の消去法" in system_prompt, (
            f"{role.name} missed the elimination rule"
        )
        assert "『白判定』と『非狼確定』を混同しない" in system_prompt, (
            f"{role.name} missed the white-vs-confirmation caveat"
        )
        assert "前提が崩れた場合は確定黒扱いを解除" in system_prompt, (
            f"{role.name} missed the breakdown / re-organize clause"
        )


async def test_ask_system_prompt_villager_seat_includes_three_seer_co_elimination_framing(
    repo: SqliteRepo,
) -> None:
    """村人席は共通ルールに加え、村人視点の framing
    (投票・発言・進行提案で残る占い師 CO 位置を狼として扱う) も system prompt
    に届く。"""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER)
    assert "残る占い師 CO 位置を投票・発言・進行提案" in system_prompt
    assert "前提が崩れた瞬間" in system_prompt


async def test_ask_system_prompt_contains_co_overflow_rule_for_any_role(
    repo: SqliteRepo,
) -> None:
    """3 役職横断 CO 数・対抗 CO 超過分推理は共通ルール経由で全 role の
    system prompt に届く。`CO 数 - 1` の式、超過分合計 3 で非 CO が確白級、
    超過分合計 0〜2 で非 CO 断定しない、超過分合計 4 以上で再整理という
    境界条件のすべてを LLM が読める状態にする。"""
    for role in (
        Role.VILLAGER,
        Role.SEER,
        Role.MEDIUM,
        Role.KNIGHT,
        Role.WEREWOLF,
        Role.MADMAN,
    ):
        system_prompt = await _capture_ask_system_prompt(repo, role)
        assert "対抗 CO 超過分" in system_prompt, f"{role.name} missed 対抗 CO 超過分"
        assert "CO 数 - 1" in system_prompt, f"{role.name} missed CO 数 - 1"
        assert "超過分合計が 3 に達した場合" in system_prompt, f"{role.name} missed sum-3 trigger"
        assert "村陣営の確白級" in system_prompt, f"{role.name} missed non-CO 確白級 consequence"
        assert "0〜2" in system_prompt, f"{role.name} missed sum 0〜2 caveat"
        assert "4 以上" in system_prompt, f"{role.name} missed sum 4+ contradiction"
        assert "配役上の消去法" in system_prompt, f"{role.name} missed 配役上の消去法 framing"


async def test_ask_system_prompt_contains_co_overflow_examples_for_any_role(
    repo: SqliteRepo,
) -> None:
    """3-2-1 / 2-2-2 / 3-1-1 / 4-1-1 の短い例も共通ルール経由で全 role に届く。"""
    for role in (
        Role.VILLAGER,
        Role.SEER,
        Role.MEDIUM,
        Role.KNIGHT,
        Role.WEREWOLF,
        Role.MADMAN,
    ):
        system_prompt = await _capture_ask_system_prompt(repo, role)
        assert "3-2-1" in system_prompt, f"{role.name} missed 3-2-1 example"
        assert "2-2-2" in system_prompt, f"{role.name} missed 2-2-2 example"
        assert "3-1-1" in system_prompt, f"{role.name} missed 3-1-1 example"
        assert "4-1-1" in system_prompt, f"{role.name} missed 4-1-1 example"


async def test_ask_system_prompt_contains_rope_margin_rules_for_any_role(
    repo: SqliteRepo,
) -> None:
    """残り縄・推定残り人狼数・吊り余裕 の共通勝ち筋ルールは、共通ルール経由で
    全 role の system prompt に届く。9 人村は残り縄のうち推定残り人狼数ぶんを
    投票で吊り切る必要があり、吊り余裕 = 残り縄 - 推定残り人狼数 が小さい
    ほど非狼濃厚位置・確白級・狂人っぽい位置を吊らない、という方針を全席に
    渡す。残り人狼数自体は秘匿情報として bot から渡されない原則も維持する。"""
    for role in (
        Role.VILLAGER,
        Role.SEER,
        Role.MEDIUM,
        Role.KNIGHT,
        Role.WEREWOLF,
        Role.MADMAN,
    ):
        system_prompt = await _capture_ask_system_prompt(repo, role)
        assert "吊り余裕" in system_prompt, f"{role.name} missed 吊り余裕"
        assert "残り縄 - 推定残り人狼数" in system_prompt, f"{role.name} missed margin formula"
        assert "推定残り人狼数" in system_prompt, f"{role.name} missed 推定残り人狼数"
        assert "投票で吊り切る" in system_prompt, f"{role.name} missed 投票で吊り切る"
        assert "吊り余裕が 0 以下" in system_prompt, f"{role.name} missed zero-margin rule"
        assert "非狼濃厚位置" in system_prompt, f"{role.name} missed 非狼濃厚位置"
        assert "敗着になり得る" in system_prompt, f"{role.name} missed 敗着"
        # Estimation source is public info, not a bot-provided secret.
        assert "秘匿情報として教える値ではなく" in system_prompt, (
            f"{role.name} missed public-info estimation framing"
        )


async def test_ask_system_prompt_villager_strategy_includes_co_overflow_action(
    repo: SqliteRepo,
) -> None:
    """村人席の system prompt には、共通ルールに加え、村人視点の運用
    (CO 群と非 CO 確白を整理し投票先を CO 群に絞る) が届く。"""
    system_prompt = await _capture_ask_system_prompt(repo, Role.VILLAGER)
    assert "対抗 CO 超過分を毎日整理する" in system_prompt
    assert "投票先を CO 群に絞る" in system_prompt


async def test_ask_system_prompt_seer_strategy_avoids_wasting_divination(
    repo: SqliteRepo,
) -> None:
    """占い師席の system prompt には、超過分合計 3 で非 CO 位置が確白級に
    なった場合に無駄占いせず対抗 CO 群やまだ確定しない位置を優先する旨が届く。"""
    system_prompt = await _capture_ask_system_prompt(repo, Role.SEER)
    assert "非 CO 確白級" in system_prompt
    assert "無駄占い" in system_prompt
    assert "対抗 CO 群やまだ確定しない位置を優先して占う" in system_prompt


async def test_ask_system_prompt_medium_strategy_updates_co_inference(
    repo: SqliteRepo,
) -> None:
    """霊媒師席の system prompt には、霊媒結果で CO 数推理を更新する運用が
    届く。霊媒白は非狼だけを示す既存ルールと整合する形 (真役職 / 狂人)。"""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MEDIUM)
    assert "霊媒結果は対抗 CO 超過分の CO 数推理を更新する材料" in system_prompt
    assert "対抗 CO 群内の狼数を絞り" in system_prompt
    assert "白なら真役職または狂人の可能性を分け" in system_prompt
    assert "非 CO 確白の前提が保たれるか" in system_prompt


async def test_ask_system_prompt_knight_strategy_protects_non_co_certified_white(
    repo: SqliteRepo,
) -> None:
    """騎士席の system prompt には、超過分合計 3 で生まれた非 CO 確白級と
    単独で対抗のない真寄り情報役を護衛価値が高いと扱う運用が届く。"""
    system_prompt = await _capture_ask_system_prompt(repo, Role.KNIGHT)
    assert "対抗 CO 超過分合計 3 で生まれた非 CO 確白級" in system_prompt
    assert "単独で対抗のない真寄り情報役は護衛価値が高い" in system_prompt


async def test_ask_system_prompt_werewolf_strategy_acknowledges_overcounter_risk(
    repo: SqliteRepo,
) -> None:
    """人狼席の system prompt には、騙りすぎで非 CO が確白級になるリスクを
    超過分集計の枠組みで認識する運用が届く。相方語彙は wolf 専用として
    ここに含まれてよい。"""
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF)
    assert "対抗 CO 超過分" in system_prompt
    assert "超過分合計が 3 に達した時点で" in system_prompt
    assert "処刑候補が CO 群に集中する" in system_prompt
    assert "相方と整合する形で選ぶ" in system_prompt


async def test_ask_system_prompt_madman_co_overflow_addition_keeps_partner_isolation(
    repo: SqliteRepo,
) -> None:
    """狂人席の system prompt には、同じリスクを公開情報視点で認識する運用が
    届く一方、wolf-coordination 語彙 (bare `相方` / `襲撃先を揃える`) は
    引き続き混入してはならず、本物の人狼位置を知っている前提の禁止文言も
    保持される (既存 leak guard との整合)。"""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MADMAN)
    # 新規 CO-overflow 文言は届く。
    assert "対抗 CO 超過分" in system_prompt
    assert "超過分合計が 3 に達した時点で" in system_prompt
    assert "処刑候補が CO 群に集中するリスクを認識する" in system_prompt
    assert "公開情報の各 CO 数と残り縄から判断する" in system_prompt
    # Wolf-coordination 語彙が漏れていないこと。
    assert not re.search(r"相方(?!候補)", system_prompt), (
        "bare '相方' (actor mode) leaked into madman system prompt via CO-overflow addition"
    )
    assert "襲撃先を揃える" not in system_prompt
    # 既存 prohibition 文言が残っていること。
    assert "人狼位置を知っている前提で話してはならない" in system_prompt


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


async def test_ask_system_prompt_wolf_seat_includes_sacrifice_value(
    repo: SqliteRepo,
) -> None:
    """End-to-end: a werewolf LLM's system prompt must carry the new 1-for-1
    trade-off (刺し違え) tactical principle and the impulsive-collapse
    rejection via `_build_strategy_block(Role.WEREWOLF)`."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF)
    assert "1 人刺し違えるだけでも人狼陣営の仕事を果たしたことになる" in system_prompt
    assert "無計画な破綻" in system_prompt


async def test_ask_system_prompt_madman_includes_hanging_value(
    repo: SqliteRepo,
) -> None:
    """End-to-end: a madman LLM's system prompt must carry the hanging-as-job
    principle and the impulsive-self-hanging rejection. The pre-existing
    wolf-positions-unknown prohibition must remain co-present so the new
    tactic stays inside the madman knowledge boundary."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MADMAN)
    assert "自分が吊られるだけでも人狼陣営の仕事を果たしたことになる" in system_prompt
    assert "無意味な自吊り" in system_prompt
    # Boundary preserved alongside the new content.
    assert "人狼位置を知っている前提で話してはならない" in system_prompt


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
    assert "5 人以上" in system_prompt
    assert "占い師と霊媒師の CO" in system_prompt
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
    assert "5 人以上" in system_prompt
    assert "占い師と霊媒師の CO" in system_prompt
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


async def test_ask_system_prompt_wolf_seat_day1_round_conditional_medium_fake(
    repo: SqliteRepo,
) -> None:
    """The werewolf system prompt must carry the day-1 round-1 medium-fake
    suppression and the day-1 round-2 conditional medium-fake (2-0 self-grey
    natural CO, or 2-1 counter-medium when forced)."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF)
    # Day-1 round-1 suppression.
    assert "day 1 の 1 巡目では霊媒師騙りをしない" in system_prompt
    # Round-2 conditional anchors.
    assert "2 巡目" in system_prompt
    assert "2-0" in system_prompt
    assert "占い師 CO 2 人" in system_prompt
    assert "霊媒師 CO 0 人" in system_prompt
    assert "自分がグレー位置" in system_prompt
    assert "投票候補" in system_prompt
    assert "自然に出た霊媒 CO" in system_prompt
    assert "対抗霊媒" in system_prompt
    assert "出ざるを得ない" in system_prompt


async def test_ask_system_prompt_madman_day1_round_conditional_medium_fake(
    repo: SqliteRepo,
) -> None:
    """The madman system prompt must carry the same day-1 round-1 medium-fake
    suppression and day-1 round-2 conditional medium-fake (2-0 self-grey
    natural CO, or 2-1 counter-medium) — plus the wolf-position-unawareness
    caveat. Wolf-coordination vocabulary must remain absent."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MADMAN)
    # Day-1 round-1 suppression.
    assert "day 1 の 1 巡目では霊媒師騙りをしない" in system_prompt
    # Round-2 conditional anchors.
    assert "2 巡目" in system_prompt
    assert "2-0" in system_prompt
    assert "自分がグレー位置" in system_prompt
    assert "投票候補" in system_prompt
    assert "自然に出た霊媒 CO" in system_prompt
    assert "対抗霊媒" in system_prompt
    assert "出ざるを得ない" in system_prompt
    # Madman-only wolf-position-unawareness caveat.
    assert "本物の狼位置を知らない" in system_prompt
    # Wolf-coordination leak guard preserved.
    assert not re.search(r"相方(?!候補)", system_prompt)
    assert "襲撃先を揃える" not in system_prompt


# -------- 2026-04-29: 3-0 (3 seer COs + 0 medium COs) → no medium fake-CO
# End-to-end mirrors of the unit-level 3-0 prohibition tests. Confirm the
# directive reaches the LLM via build_system_prompt for wolf and madman seats,
# stays out of non-wolf seats, and that the day-1 round-2 task block carries
# the prohibition when `_do_one_discussion_speech(discussion_round=2)` runs.


async def test_ask_system_prompt_wolf_seat_includes_3_0_no_medium_fake(
    repo: SqliteRepo,
) -> None:
    """End-to-end: a werewolf LLM's system prompt must carry the 3-0 medium-
    fake prohibition via `_ROLE_STRATEGIES[Role.WEREWOLF]`."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF)
    assert "3-0" in system_prompt
    assert "占い師 CO 3 人" in system_prompt
    assert "霊媒師 CO 0 人" in system_prompt
    assert "絶対に霊媒師 CO しない" in system_prompt
    assert "真霊媒師の対抗 CO" in system_prompt
    assert "3-2" in system_prompt
    assert "超過分合計" in system_prompt
    assert "確白級" in system_prompt
    assert "処刑候補が CO 群へ集中" in system_prompt
    # Already-CO'd-seer continuation must be explicitly allowed.
    assert "既に自分が占い師 CO 中なら" in system_prompt


async def test_ask_system_prompt_madman_includes_3_0_no_medium_fake(
    repo: SqliteRepo,
) -> None:
    """End-to-end madman analog with the wolf-position-unawareness anchor.
    Wolf-coordination vocabulary must stay absent (existing leak guard)."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MADMAN)
    assert "3-0" in system_prompt
    assert "占い師 CO 3 人" in system_prompt
    assert "霊媒師 CO 0 人" in system_prompt
    assert "絶対に霊媒師 CO しない" in system_prompt
    assert "3-2" in system_prompt
    assert "超過分合計" in system_prompt
    assert "確白級" in system_prompt
    assert "処刑候補が CO 群へ集中" in system_prompt
    assert "本物の人狼位置を知らない" in system_prompt
    # Already-CO'd-seer continuation explicitly allowed.
    assert "既に自分が占い師 CO 中なら" in system_prompt
    # Leak guard.
    assert not re.search(r"相方(?!候補)", system_prompt)
    assert "襲撃先を揃える" not in system_prompt


@pytest.mark.parametrize("role", [Role.SEER, Role.MEDIUM, Role.KNIGHT, Role.VILLAGER])
async def test_ask_system_prompt_non_wolf_excludes_3_0_directive(
    repo: SqliteRepo,
    role: Role,
) -> None:
    """The 3-0 wolf-side prohibition must not bleed into non-wolf/non-madman
    seats' system prompts. Mirrors the unit-level cross-leak guard."""
    system_prompt = await _capture_ask_system_prompt(repo, role)
    assert "絶対に霊媒師 CO しない" not in system_prompt, (
        f"wolf-side 3-0 prohibition leaked into {role.name}"
    )


async def test_ask_system_prompt_medium_includes_day1_co_timing(
    repo: SqliteRepo,
) -> None:
    """The true medium's system prompt must carry the day-1 round-1 silence
    and the day-1 round-2 grey-CO / counter-CO directives. day-1 medium CO
    must explicitly carry no execution result. Wolf-coordination vocabulary
    must remain absent from the medium seat."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MEDIUM)
    # Round-1 silence.
    assert "day 1 の 1 巡目では霊媒 CO しない" in system_prompt
    # Round-2 grey CO anchors.
    assert "2 巡目" in system_prompt
    assert "2-0" in system_prompt
    assert "占い師 CO 2 人" in system_prompt
    assert "霊媒師 CO 0 人" in system_prompt
    assert "自分がグレー位置" in system_prompt
    assert "投票候補を狭め" in system_prompt
    # Round-2 counter-CO on fake medium.
    assert "霊媒騙りが出た場合" in system_prompt
    assert "当然対抗 CO" in system_prompt
    # Day-1 has no execution → no medium result.
    assert "まだ処刑がないため霊媒結果はない" in system_prompt
    # Wolf-coordination leak guard.
    assert not re.search(r"相方(?!候補)", system_prompt)
    assert "襲撃先を揃える" not in system_prompt


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


# ----------------------------- 2026-04-28: 占い無駄削減 / 過剰騙り抑止 / 騎士 day 別 CO
# End-to-end mirrors of the unit-level prompt-builder tests for the same three
# additions: seer + SEER_DIVINE no-waste, wolf/madman 3-seer-CO no-extra-fake,
# and knight day-1/2/3 conditional CO. These exercise build_system_prompt via
# `_capture_ask_system_prompt` so the production path is asserted, not just the
# pure helpers.


async def test_ask_system_prompt_seer_avoids_wasting_on_confirmed_white(
    repo: SqliteRepo,
) -> None:
    """The seer's system prompt must carry the no-waste-divination guidance:
    skip 確定白 / 非 CO 確白級 / progression-role-eligible positions, while
    keeping the 白判定 vs 確定白 distinction (狂人 reads white too)."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.SEER)
    assert "確定白" in system_prompt
    assert "非 CO 確白級" in system_prompt
    assert "無駄占い" in system_prompt
    assert "信用が未確定" in system_prompt
    assert "単発白" in system_prompt
    assert "狂人も白に出る" in system_prompt


async def test_ask_system_prompt_seer_divine_task_avoids_wasted_divination(
    repo: SqliteRepo,
) -> None:
    """When the seer's task_text is the actual SEER_DIVINE prompt, the no-waste
    tokens (確定白 / 非 CO 確白級 / 無駄占い / グレー / 対抗 CO 群) must reach
    the LLM via the `{task_block}` slot. Existing targeting-axes tokens stay."""
    task_text = task_night_action(SubmissionType.SEER_DIVINE, ["席1 A", "席2 B"])
    system_prompt = await _capture_ask_system_prompt(repo, Role.SEER, task_text=task_text)
    # New no-waste tokens reached the LLM.
    assert "確定白" in system_prompt
    assert "非 CO 確白級" in system_prompt
    assert "無駄占い" in system_prompt
    assert "グレー" in system_prompt
    assert "対抗 CO 群" in system_prompt
    # Existing axes preserved (no regression).
    assert "占い価値" in system_prompt
    assert "囲い候補" in system_prompt
    assert "白でも黒でも情報が落ちる" in system_prompt


async def test_ask_system_prompt_wolf_avoids_extra_fakes_under_three_seer_co(
    repo: SqliteRepo,
) -> None:
    """The werewolf's system prompt must carry the no-extra-fake rule for
    3-seer-CO boards: do not add medium/knight fakes — pushing CO overflow to
    ≥3 hardens non-CO seats into 村陣営の確白級 and concentrates the village's
    hangs onto the CO group."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF)
    assert "占い師 CO が 3 人" in system_prompt
    assert "霊媒師騙りや騎士騙りを追加しない" in system_prompt
    assert "非 CO 位置" in system_prompt
    assert "村陣営の確白級" in system_prompt
    assert "処刑候補が CO 群に集中" in system_prompt
    # Wolf-actor partner vocabulary remains usable in the wolf seat.
    assert "相方" in system_prompt


async def test_ask_system_prompt_madman_avoids_extra_fakes_under_three_seer_co(
    repo: SqliteRepo,
) -> None:
    """The madman's system prompt must carry the same no-extra-fake rule with
    public-information-only framing (本物の狼位置を知らない) and must not leak
    wolf-coordination vocabulary. Existing prohibition phrase remains."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MADMAN)
    assert "占い師 CO が 3 人" in system_prompt
    assert "霊媒師騙りや騎士騙りを追加しない" in system_prompt
    assert "本物の狼位置を知らない" in system_prompt
    assert "非 CO 確白" in system_prompt
    assert "処刑候補を狭める" in system_prompt
    # Existing leak guard preserved. Bare 相方 (actor mode) absent;
    # 相方候補 (public-log inference) allowed.
    assert not re.search(r"相方(?!候補)", system_prompt)
    assert "襲撃先を揃える" not in system_prompt
    # Existing prohibition phrase remains.
    assert "人狼位置を知っている前提で話してはならない" in system_prompt


async def test_ask_system_prompt_knight_includes_day_conditional_co(
    repo: SqliteRepo,
) -> None:
    """The knight's system prompt must carry the day-1 round-2 2-1 grey-4 CO
    rule, the day-2 not-confirmed-white + guard-success CO rule, and the
    day-3 not-confirmed-white CO rule. Existing knight peaceful-morning and
    endgame CO guidance must remain alongside (no regression)."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.KNIGHT)
    # day 1 round 2 2-1 grey-4 CO.
    assert "day 1" in system_prompt
    assert "2 巡目" in system_prompt
    assert "2-1" in system_prompt
    assert "グレーが 4 人" in system_prompt
    assert "自分がそのグレー位置なら" in system_prompt
    assert "投票位置を 4 人から 3 人" in system_prompt
    assert "捏造しない" in system_prompt
    # day 2 not-confirmed-white + guard-success CO.
    assert "day 2" in system_prompt
    assert "自分が確定白ではなく" in system_prompt
    assert "護衛成功した場合" in system_prompt
    assert "機械的に CO しない" in system_prompt
    # day 3 not-confirmed-white CO.
    assert "day 3" in system_prompt
    assert "自分が確定白でないなら" in system_prompt
    assert "対抗騎士" in system_prompt
    # Existing knight unique anchor remains.
    assert "前夜と違う相手を選ぶ" in system_prompt
    # Wolf-coordination leak guard preserved.
    assert not re.search(r"相方(?!候補)", system_prompt)
    assert "襲撃先を揃える" not in system_prompt


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


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
async def test_discussion_speech_day1_round1_wolf_suppresses_medium_fake(
    repo: SqliteRepo,
    role: Role,
) -> None:
    """End-to-end: when `_do_one_discussion_speech(discussion_round=1)` runs
    on a day-1 game with a wolf or madman LLM seat, the captured task block
    must include the day-1 round-1 medium-fake suppression directive — and
    the old 3-way (seer/medium/lurk) phrasing must be fully removed."""
    game = Game(
        id=f"g-d1r1-w-{role.value}",
        guild_id=f"gu-d1r1-w-{role.value}",
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
    await repo.set_player_role(game.id, 2, role)

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
    # Day-1 round-1 suppression directive must reach the LLM via the task block.
    assert "day 1 の 1 巡目では霊媒師騙りをしない" in system_prompt
    # Old 3-way (seer/medium/lurk) phrasing must be fully removed.
    assert "占い師騙り・霊媒師騙り・潜伏の 3 択" not in system_prompt


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
async def test_discussion_speech_day1_round2_wolf_includes_conditional_medium_fake(
    repo: SqliteRepo,
    role: Role,
) -> None:
    """End-to-end: when `_do_one_discussion_speech(discussion_round=2)` runs
    on a day-1 game with a wolf or madman LLM seat, the captured task block
    must include the day-1 round-2 conditional medium-fake guidance: 2-0
    self-grey natural CO, or 2-1 counter-medium when forced. day-1 has no
    execution → no medium result allowed."""
    game = Game(
        id=f"g-d1r2-w-{role.value}",
        guild_id=f"gu-d1r2-w-{role.value}",
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
    await repo.set_player_role(game.id, 2, role)

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
    # Round-2 conditional anchors must reach the LLM via the task block.
    assert "2-0" in system_prompt
    assert "2-1" in system_prompt
    assert "自分がグレー位置" in system_prompt
    assert "投票候補" in system_prompt
    assert "自然に出た霊媒 CO" in system_prompt
    assert "対抗霊媒" in system_prompt
    assert "出ざるを得ない" in system_prompt
    # Day-1 has no execution → no medium result.
    assert "霊媒結果は出さず" in system_prompt


@pytest.mark.parametrize("role", [Role.WEREWOLF, Role.MADMAN])
async def test_discussion_speech_day1_round2_wolf_includes_3_0_no_medium_fake(
    repo: SqliteRepo,
    role: Role,
) -> None:
    """End-to-end: when `_do_one_discussion_speech(discussion_round=2)` runs
    on a day-1 game with a wolf or madman LLM seat, the captured task block
    must include the 3-0 medium-fake prohibition (3 seer COs + 0 medium COs
    → no medium CO, even when self-grey)."""
    game = Game(
        id=f"g-d1r2-30-{role.value}",
        guild_id=f"gu-d1r2-30-{role.value}",
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
    await repo.set_player_role(game.id, 2, role)

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
    # Task-level 3-0 directive reached the LLM via the task block.
    assert "3-0" in system_prompt
    assert "占い師 CO 3 人" in system_prompt
    assert "霊媒師 CO 0 人" in system_prompt
    assert "絶対に霊媒師 CO しない" in system_prompt
    assert "3-2" in system_prompt
    assert "確白級" in system_prompt


async def test_discussion_speech_day1_round1_medium_lurks(
    repo: SqliteRepo,
) -> None:
    """End-to-end: at day-1 round-1, the true medium gets an explicit don't-CO
    directive via the captured task block. Wolf-side phrasing must not leak."""
    game = Game(
        id="g-d1r1-medium",
        guild_id="gu-d1r1-medium",
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
    await repo.set_player_role(game.id, 2, Role.MEDIUM)

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
    # Medium round-1 silence directive must reach the LLM via the task block.
    assert "day 1 の 1 巡目では霊媒 CO しない" in system_prompt
    # Wolf-side phrasing must not leak into medium task block.
    assert "占い師騙り・霊媒師騙り・潜伏の 3 択" not in system_prompt


async def test_discussion_speech_day1_round2_medium_co_or_counter(
    repo: SqliteRepo,
) -> None:
    """End-to-end: at day-1 round-2, the true medium gets the 2-0 self-grey
    CO directive plus a counter-CO directive if a medium-fake surfaces.
    Day-1 has no execution → no medium result."""
    game = Game(
        id="g-d1r2-medium",
        guild_id="gu-d1r2-medium",
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
    await repo.set_player_role(game.id, 2, Role.MEDIUM)

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
    # Round-2 medium CO / counter-CO directives must reach the LLM.
    assert "2-0" in system_prompt
    assert "自分がグレー位置" in system_prompt
    assert "霊媒 CO" in system_prompt
    assert "霊媒騙りが出た場合" in system_prompt
    assert "当然対抗 CO" in system_prompt
    # Day-1 has no execution → no medium result.
    assert "霊媒結果はありません" in system_prompt


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
    `system_instruction`, user context as `contents`. Sampling controls do NOT
    appear as top-level kwargs (Gemini puts `temperature` on `config` instead;
    the `config.temperature` flow is exercised by a sibling test). DeepSeek-only
    knobs (`extra_body`, `reasoning_effort`) must NOT be sent at all."""
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


async def test_gemini_decider_forwards_temperature_to_generate_content_config() -> None:
    """Gemini path: a `temperature` constructor argument must reach
    `GenerateContentConfig.temperature`. This is the only Gemini sampling
    control wolfbot exposes — xAI / DeepSeek deliberately don't get one."""
    from wolfbot.services.llm_service import GeminiLLMActionDecider

    fake = _FakeGenAIClient(_canned_action_json())
    decider = GeminiLLMActionDecider(
        client=fake,
        model="gemini-3-flash-preview",
        thinking_level="high",
        temperature=0.7,
        timeout=15.0,
    )
    await decider.decide("sys", "ctx")

    config = fake.aio.models.calls[0]["config"]
    assert config.temperature == 0.7


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


def test_make_llm_decider_gemini_branch_passes_temperature_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end factory wiring: `settings.GEMINI_TEMPERATURE` must reach the
    constructed `GeminiLLMActionDecider.temperature`. The xAI and DeepSeek
    branches still don't accept a sampling-control knob (no symmetrical test
    needed — they have no temperature parameter to assert)."""
    from pydantic import SecretStr

    from wolfbot.config import Settings
    from wolfbot.services.llm_service import GeminiLLMActionDecider, make_llm_decider

    class _StubClient:
        def __init__(self, **kwargs: object) -> None:
            pass

    import google.genai

    monkeypatch.setattr(google.genai, "Client", _StubClient)

    s = Settings(  # type: ignore[arg-type]
        _env_file=None,
        DISCORD_TOKEN=SecretStr("t"),
        DISCORD_GUILD_ID=1,
        MAIN_TEXT_CHANNEL_ID=2,
        MAIN_VOICE_CHANNEL_ID=3,
        LLM_PROVIDER="gemini",
        GEMINI_VERTEX_PROJECT="my-project",
        GEMINI_TEMPERATURE=0.3,
    )
    decider = make_llm_decider(s)
    assert isinstance(decider, GeminiLLMActionDecider)
    assert decider.temperature == 0.3


# ============================================================
# DAY_EXECUTION_SPEECH (LLM last words) — submit + per-seat run
# ============================================================
async def _seed_execution_speech_game(repo: SqliteRepo) -> tuple[Game, list[Seat]]:
    """One LLM seat (seat 2, role=SEER) parked in DAY_EXECUTION_SPEECH on day 1.

    The seat is the executed LLM about to give last words. Other seats are
    irrelevant to this dispatch path so we keep them minimal.
    """
    game = Game(
        id="g-exec-speech",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_EXECUTION_SPEECH,
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
    return game, seats


async def test_submit_llm_execution_speech_is_fire_and_forget(repo: SqliteRepo) -> None:
    """Caller returns immediately; xAI work happens in a background task."""
    game, seats = await _seed_execution_speech_game(repo)
    release = asyncio.Event()
    decider = _BlockingDecider(
        [
            LLMAction(
                intent="speak",
                public_message="最後に村の方々へ、占い結果を残します。",
                reason_summary="",
                confidence=0.9,
            )
        ],
        release,
    )
    poster = _FakePoster()
    adapter = LLMAdapter(repo=repo, decider=decider, message_poster=poster, rng=random.Random(0))
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    # The submit call must return promptly even though decider blocks.
    await asyncio.wait_for(
        adapter.submit_llm_execution_speech(game, players, seats, executed_seat=2),
        timeout=0.5,
    )
    # Allow the background task to start (it will block on `release`).
    await asyncio.sleep(0.05)
    # No public post yet — decider hasn't returned.
    assert poster.public == []
    # Release and drain the task.
    release.set()
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)
    assert len(poster.public) == 1


async def test_execution_speech_speak_intent_posts_and_logs(repo: SqliteRepo) -> None:
    """speak + non-empty message → post_public + PLAYER_SPEECH log."""
    game, seats = await _seed_execution_speech_game(repo)
    decider = _ScriptedDecider(
        [
            LLMAction(
                intent="speak",
                public_message="占い師 CO。day 0 ランダム白は H1。day 1 朝の発言で 3 を黒予想。",
                reason_summary="",
                confidence=0.9,
            )
        ]
    )
    poster = _FakePoster()
    adapter = LLMAdapter(
        repo=repo, decider=decider, message_poster=poster, rng=random.Random(0), clock=lambda: 555
    )
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_execution_speech(game, players, seats, executed_seat=2)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert len(poster.public) == 1
    body = poster.public[0][1]
    assert "セツ" in body and "占い師 CO" in body
    logs = await repo.load_public_logs(game.id, limit=40)
    speech_rows = [r for r in logs if r.get("kind") == "PLAYER_SPEECH"]
    assert len(speech_rows) == 1
    assert speech_rows[0].get("actor_seat") == 2
    # Progress marked.
    assert await repo.load_llm_execution_speech_done(game.id, day=1, seat_no=2) is True


async def test_execution_speech_skip_intent_marks_done_without_posting(
    repo: SqliteRepo,
) -> None:
    """intent=skip is allowed (rare) — nothing is posted, but the seat must
    still be marked done so the engine can advance."""
    game, seats = await _seed_execution_speech_game(repo)
    decider = _ScriptedDecider(
        [LLMAction(intent="skip", reason_summary="nothing useful to add", confidence=0.3)]
    )
    poster = _FakePoster()
    adapter = LLMAdapter(repo=repo, decider=decider, message_poster=poster, rng=random.Random(0))
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_execution_speech(game, players, seats, executed_seat=2)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert poster.public == []
    logs = await repo.load_public_logs(game.id, limit=40)
    assert [r for r in logs if r.get("kind") == "PLAYER_SPEECH"] == []
    assert await repo.load_llm_execution_speech_done(game.id, day=1, seat_no=2) is True


async def test_execution_speech_empty_message_marks_done_without_posting(
    repo: SqliteRepo,
) -> None:
    """speak with empty/whitespace message → no post but progress marked."""
    game, seats = await _seed_execution_speech_game(repo)
    decider = _ScriptedDecider(
        [LLMAction(intent="speak", public_message="   ", reason_summary="", confidence=0.5)]
    )
    poster = _FakePoster()
    adapter = LLMAdapter(repo=repo, decider=decider, message_poster=poster, rng=random.Random(0))
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_execution_speech(game, players, seats, executed_seat=2)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert poster.public == []
    assert await repo.load_llm_execution_speech_done(game.id, day=1, seat_no=2) is True


class _ExplodingDecider:
    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        raise RuntimeError("simulated decider failure")


async def test_execution_speech_decider_exception_still_marks_done(
    repo: SqliteRepo,
) -> None:
    """Decider raises → caught, progress still set so engine can advance."""
    game, seats = await _seed_execution_speech_game(repo)
    poster = _FakePoster()
    adapter = LLMAdapter(
        repo=repo, decider=_ExplodingDecider(), message_poster=poster, rng=random.Random(0)
    )
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_execution_speech(game, players, seats, executed_seat=2)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    # _ask catches decider exceptions and returns intent=skip, so no post.
    # Progress is marked in the run loop's finally regardless.
    assert poster.public == []
    assert await repo.load_llm_execution_speech_done(game.id, day=1, seat_no=2) is True


async def test_execution_speech_stale_phase_no_post(repo: SqliteRepo) -> None:
    """If the game has already advanced past DAY_EXECUTION_SPEECH (e.g. host
    force-skipped) by the time the background task runs, the speech is dropped."""
    game, seats = await _seed_execution_speech_game(repo)
    # Move the game out of DAY_EXECUTION_SPEECH before dispatch — the background
    # task will reload `fresh` and bail.
    async with repo._db.execute(  # type: ignore[attr-defined]
        "UPDATE games SET phase=? WHERE id=?",
        (Phase.NIGHT.value, game.id),
    ):
        pass
    await repo._db.commit()  # type: ignore[attr-defined]

    decider = _ScriptedDecider(
        [
            LLMAction(
                intent="speak", public_message="should not post", reason_summary="", confidence=0.9
            )
        ]
    )
    poster = _FakePoster()
    adapter = LLMAdapter(repo=repo, decider=decider, message_poster=poster, rng=random.Random(0))
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_execution_speech(game, players, seats, executed_seat=2)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    # No post; no progress mark either (the early-bail path doesn't write
    # progress because the game isn't in DAY_EXECUTION_SPEECH any more — the
    # mark only fires after a real attempt or skipped persona).
    assert poster.public == []


async def test_execution_speech_already_done_skips_redispatch(repo: SqliteRepo) -> None:
    """Recovery overlap / double-wake: if execution_speech_done is already True,
    re-dispatch is a no-op (no decider call, no post)."""
    game, seats = await _seed_execution_speech_game(repo)
    await repo.mark_llm_execution_speech_done(game.id, day=1, seat_no=2)
    decider = _ScriptedDecider([])  # empty: any call would IndexError
    poster = _FakePoster()
    adapter = LLMAdapter(repo=repo, decider=decider, message_poster=poster, rng=random.Random(0))
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_execution_speech(game, players, seats, executed_seat=2)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert poster.public == []


async def test_execution_speech_skipped_for_human_seat(repo: SqliteRepo) -> None:
    """If executed_seat happens to be human, the dispatcher no-ops without
    invoking the decider — the planner shouldn't have parked in
    DAY_EXECUTION_SPEECH for a human, but defense-in-depth keeps us safe."""
    game, seats = await _seed_execution_speech_game(repo)
    decider = _ScriptedDecider([])  # empty: any call would IndexError
    poster = _FakePoster()
    adapter = LLMAdapter(repo=repo, decider=decider, message_poster=poster, rng=random.Random(0))
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_execution_speech(game, players, seats, executed_seat=1)  # H1
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert poster.public == []
    # No flag set on the human seat either.
    assert await repo.load_llm_execution_speech_done(game.id, day=1, seat_no=1) is False


async def test_execution_speech_uses_task_last_words_text(repo: SqliteRepo) -> None:
    """The decider receives a system prompt whose task block matches
    `task_last_words(day, role=...)` — verifies role-specific guidance flows."""
    from wolfbot.llm.prompt_builder import task_last_words

    game, seats = await _seed_execution_speech_game(repo)
    captured: list[tuple[str, str]] = []

    class _Capturing:
        async def decide(self, system: str, user: str) -> LLMAction:
            captured.append((system, user))
            return LLMAction(intent="skip", reason_summary="", confidence=0.5)

    poster = _FakePoster()
    adapter = LLMAdapter(
        repo=repo, decider=_Capturing(), message_poster=poster, rng=random.Random(0)
    )
    adapter.set_game_service(_FakeGameService())  # type: ignore[arg-type]
    players = await repo.load_players(game.id)

    await adapter.submit_llm_execution_speech(game, players, seats, executed_seat=2)
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert len(captured) == 1
    system_prompt = captured[0][0]
    # Seer-specific guidance from task_last_words must be present in the task block.
    assert "占い師 CO" in system_prompt
    # Cross-check: the helper itself produced the same line.
    assert "占い師 CO" in task_last_words(1, role=Role.SEER)
