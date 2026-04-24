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
from wolfbot.domain.models import Game, LogEntry, NightAction, Seat, Vote
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

    await adapter._maybe_speak(game, llm_player, llm_seat, seats)

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
    repo: SqliteRepo, role: Role, *, persona_key: str = "setsu"
) -> str:
    """Seed a tiny game with one LLM seat of the given role, invoke `_ask`,
    and return the captured system prompt. The role-specific strategy and the
    shared rules block are injected inside `build_system_prompt`, which `_ask`
    calls per-seat, so this exercises the exact production path.

    `persona_key` lets callers capture the prompt for any persona; the default
    preserves behavior for all pre-existing callers. Both `game_id` and
    `guild_id` are scoped by (role, persona_key) so multiple calls in one test
    don't collide on the shared repo or trip the partial-unique-index
    ("at most one active game per guild").
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

    await adapter._ask(game, me, my_seat, players, seats, task_text="test-task")

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


async def test_ask_system_prompt_wolf_seat_includes_wolf_strategy(repo: SqliteRepo) -> None:
    """A werewolf LLM must receive wolf-coordination tips in its system
    prompt (`相方`, `襲撃先を揃える`)."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.WEREWOLF)
    assert "相方" in system_prompt
    assert "襲撃先を揃える" in system_prompt


async def test_ask_system_prompt_non_wolf_excludes_wolf_strategy(repo: SqliteRepo) -> None:
    """A non-wolf LLM must NOT receive wolf-coordination tips. This guards
    against strategy leakage through `build_system_prompt`."""
    for role in (Role.SEER, Role.MEDIUM, Role.KNIGHT, Role.VILLAGER, Role.MADMAN):
        system_prompt = await _capture_ask_system_prompt(repo, role)
        assert "相方" not in system_prompt, f"{role.name} saw '相方' in system prompt"
        assert "襲撃先を揃える" not in system_prompt, (
            f"{role.name} saw '襲撃先を揃える' in system prompt"
        )


async def test_ask_system_prompt_madman_excludes_wolf_positions_assumption(
    repo: SqliteRepo,
) -> None:
    """The madman must be told explicitly NOT to assume real wolf positions,
    and must NOT receive wolf-coordination tips. The prohibition phrase must
    be present; the wolf playbook vocabulary must not."""
    system_prompt = await _capture_ask_system_prompt(repo, Role.MADMAN)
    assert "人狼位置を知っている前提で話してはならない" in system_prompt
    assert "相方" not in system_prompt
    assert "襲撃先を揃える" not in system_prompt


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
            assert "相方" not in system_prompt, f"{role.name}/{pkey} saw '相方' in system prompt"
            assert "襲撃先を揃える" not in system_prompt, (
                f"{role.name}/{pkey} saw '襲撃先を揃える' in system prompt"
            )
