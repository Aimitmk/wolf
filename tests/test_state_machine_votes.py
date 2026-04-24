"""State machine: DAY_VOTE and DAY_RUNOFF resolution."""

from __future__ import annotations

from wolfbot.domain.enums import DeathCause, Phase, Role, SubmissionType
from wolfbot.domain.models import Game, Player, Seat, Vote
from wolfbot.domain.state_machine import (
    NIGHT_DURATION,
    RUNOFF_DURATION,
    plan_day_runoff_resolve,
    plan_day_vote_resolve,
)


def _game(phase: Phase = Phase.DAY_VOTE, day: int = 1) -> Game:
    return Game(
        id="g1",
        guild_id="gu1",
        host_user_id="h1",
        phase=phase,
        day_number=day,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )


def _players(roles: list[Role], alive: list[bool] | None = None) -> list[Player]:
    ps = []
    for i, r in enumerate(roles, start=1):
        live = True if alive is None else alive[i - 1]
        ps.append(Player(seat_no=i, role=r, alive=live))
    return ps


def _seats(n: int = 9) -> list[Seat]:
    return [
        Seat(
            seat_no=i, display_name=f"P{i}", discord_user_id=f"u{i}", is_llm=False, persona_key=None
        )
        for i in range(1, n + 1)
    ]


STANDARD_ROLES = [
    Role.WEREWOLF,
    Role.WEREWOLF,
    Role.MADMAN,
    Role.SEER,
    Role.MEDIUM,
    Role.KNIGHT,
    Role.VILLAGER,
    Role.VILLAGER,
    Role.VILLAGER,
]


def _v(voter: int, target: int | None, day: int = 1, round_: int = 0) -> Vote:
    return Vote(
        game_id="g1",
        day=day,
        round=round_,
        voter_seat=voter,
        target_seat=target,
        submitted_at=0,
    )


# ---------------------------------------------------------------- DAY_VOTE
def test_unique_plurality_executes_and_advances_to_night() -> None:
    game = _game(day=1)
    players = _players(STANDARD_ROLES)
    seats = _seats()
    votes = [
        _v(1, 7),
        _v(2, 7),
        _v(3, 7),
        _v(4, 7),
        _v(5, 8),
        _v(6, 8),
        _v(7, 9),
        _v(8, 9),
        _v(9, 1),
    ]
    t = plan_day_vote_resolve(game, players, seats, votes, force_skip=False, now_epoch=1000)
    # Executed seat 7 → NIGHT
    assert t.next_phase is Phase.NIGHT
    assert t.new_deadline_epoch == 1000 + NIGHT_DURATION
    updates = {u.seat_no: u for u in t.player_updates}
    assert 7 in updates and updates[7].alive is False
    assert updates[7].death_cause is DeathCause.EXECUTION
    assert t.newly_dead_seats == (7,)


def test_tied_vote_goes_to_runoff() -> None:
    game = _game(day=1)
    players = _players(STANDARD_ROLES)
    seats = _seats()
    votes = [
        _v(1, 7),
        _v(2, 7),
        _v(3, 8),
        _v(4, 8),
        _v(5, 9),
        _v(6, 9),
        _v(7, 9),  # 9 gets 3 votes
        _v(8, 7),  # now 7 also has 3 votes
        _v(9, 1),
    ]
    t = plan_day_vote_resolve(game, players, seats, votes, force_skip=False, now_epoch=1000)
    assert t.next_phase is Phase.DAY_RUNOFF
    assert t.new_deadline_epoch == 1000 + RUNOFF_DURATION
    assert t.player_updates == ()


def test_missing_vote_pauses_when_no_force_skip() -> None:
    game = _game(day=1)
    players = _players(STANDARD_ROLES)
    seats = _seats()
    votes = [_v(1, 7), _v(2, 7)]  # seats 3..9 didn't vote
    t = plan_day_vote_resolve(game, players, seats, votes, force_skip=False, now_epoch=1000)
    assert t.next_phase is Phase.WAITING_HOST_DECISION
    assert t.requires_host_decision is True
    assert t.pending is not None
    assert t.pending.required_submission is SubmissionType.VOTE
    assert t.pending.missing_seats == (3, 4, 5, 6, 7, 8, 9)


def test_missing_vote_with_force_skip_treats_missing_as_abstain() -> None:
    game = _game(day=1)
    players = _players(STANDARD_ROLES)
    seats = _seats()
    votes = [_v(1, 7), _v(2, 7), _v(3, 7)]
    t = plan_day_vote_resolve(game, players, seats, votes, force_skip=True, now_epoch=1000)
    # 7 is only target with 3 votes → executed
    assert t.next_phase is Phase.NIGHT
    assert any(u.seat_no == 7 and u.alive is False for u in t.player_updates)


def test_all_abstain_no_execution_goes_to_night() -> None:
    game = _game(day=1)
    players = _players(STANDARD_ROLES)
    seats = _seats()
    votes = [_v(i, None) for i in range(1, 10)]
    t = plan_day_vote_resolve(game, players, seats, votes, force_skip=False, now_epoch=1000)
    assert t.next_phase is Phase.NIGHT
    assert t.player_updates == ()
    assert t.newly_dead_seats == ()


# ---------------------------------------------------------------- DAY_RUNOFF
def test_runoff_tie_skips_execution() -> None:
    game = _game(phase=Phase.DAY_RUNOFF, day=1)
    players = _players(STANDARD_ROLES)
    seats = _seats()
    votes = [
        _v(1, 7, round_=1),
        _v(2, 7, round_=1),
        _v(3, 8, round_=1),
        _v(4, 8, round_=1),
        _v(5, 7, round_=1),
        _v(6, 8, round_=1),
        _v(7, 7, round_=1),
        _v(8, 8, round_=1),
        _v(9, None, round_=1),
    ]
    t = plan_day_runoff_resolve(
        game,
        players,
        seats,
        votes,
        tied_candidates=[7, 8],
        force_skip=False,
        now_epoch=2000,
    )
    # 7 and 8 tied again → no execution, go to NIGHT
    assert t.next_phase is Phase.NIGHT
    assert t.player_updates == ()
    assert t.newly_dead_seats == ()


def test_runoff_unique_executes() -> None:
    game = _game(phase=Phase.DAY_RUNOFF, day=1)
    players = _players(STANDARD_ROLES)
    seats = _seats()
    votes = [
        _v(1, 7, round_=1),
        _v(2, 7, round_=1),
        _v(3, 7, round_=1),
        _v(4, 8, round_=1),
        _v(5, 8, round_=1),
        _v(6, 7, round_=1),
        _v(7, 8, round_=1),
        _v(8, 7, round_=1),
        _v(9, 8, round_=1),
    ]
    t = plan_day_runoff_resolve(
        game,
        players,
        seats,
        votes,
        tied_candidates=[7, 8],
        force_skip=False,
        now_epoch=2000,
    )
    # 7 gets 5, 8 gets 4 → 7 executed
    assert t.next_phase is Phase.NIGHT
    assert any(u.seat_no == 7 and u.alive is False for u in t.player_updates)


def test_execution_triggering_victory_ends_game() -> None:
    """Wolves dead → VILLAGE wins, next_phase=GAME_OVER."""
    game = _game(phase=Phase.DAY_VOTE, day=3)
    # Leave only 1 wolf alive, execute them
    alive = [True, False, False, False, False, True, True, True, True]
    # seats 1 = alive wolf; others dead except 6,7,8,9 villagers/knight
    players = _players(STANDARD_ROLES, alive=alive)
    seats = _seats()
    # Vote everyone against seat 1
    votes = [_v(1, 1), _v(6, 1), _v(7, 1), _v(8, 1), _v(9, 1)]
    # self-vote for 1 is dropped; voters 6,7,8,9 vote 1 → 4 votes
    t = plan_day_vote_resolve(game, players, seats, votes, force_skip=False, now_epoch=3000)
    assert t.next_phase is Phase.GAME_OVER
    assert t.victory is not None
    assert t.new_deadline_epoch is None


def test_execution_victory_emits_role_reveal_after_victory() -> None:
    """On execution-path victory, ROLE_REVEAL follows VICTORY with every seat listed."""
    game = _game(phase=Phase.DAY_VOTE, day=3)
    alive = [True, False, False, False, False, True, True, True, True]
    players = _players(STANDARD_ROLES, alive=alive)
    seats = _seats()
    votes = [_v(1, 1), _v(6, 1), _v(7, 1), _v(8, 1), _v(9, 1)]
    t = plan_day_vote_resolve(game, players, seats, votes, force_skip=False, now_epoch=3000)
    assert t.next_phase is Phase.GAME_OVER

    kinds = [lg.kind for lg in t.public_logs]
    assert "VICTORY" in kinds
    assert "ROLE_REVEAL" in kinds
    # VICTORY must precede ROLE_REVEAL and be the immediate neighbor.
    victory_idx = kinds.index("VICTORY")
    reveal_idx = kinds.index("ROLE_REVEAL")
    assert reveal_idx == victory_idx + 1

    reveal = t.public_logs[reveal_idx]
    assert reveal.text.startswith("最終配役:\n")
    # Every seat (1–9) appears exactly once with role + alive/dead tag.
    for seat_no in range(1, 10):
        assert f"- 席{seat_no} P{seat_no}:" in reveal.text
    # Seat 1 (wolf) was the one executed just now → must show 死亡.
    assert "- 席1 P1: 人狼 (死亡)" in reveal.text
    # Seat 3 (madman) was already dead before this execution → 死亡.
    assert "- 席3 P3: 狂人 (死亡)" in reveal.text
    # Seat 6 (knight) is alive.
    assert "- 席6 P6: 騎士 (生存)" in reveal.text


# ---------------------------------------------------------------- vote tally
def test_execution_log_includes_grouped_vote_tally() -> None:
    """EXECUTION text appends 🗳 投票結果 block grouped by target, sorted by count."""
    game = _game(day=1)
    players = _players(STANDARD_ROLES)
    seats = _seats()
    votes = [
        _v(1, 7),
        _v(2, 7),
        _v(3, 7),
        _v(4, 7),
        _v(5, 8),
        _v(6, 8),
        _v(7, 9),
        _v(8, None),
        _v(9, None),
    ]
    t = plan_day_vote_resolve(game, players, seats, votes, force_skip=False, now_epoch=1000)
    exec_log = next(log for log in t.public_logs if log.kind == "EXECUTION")
    text = exec_log.text
    assert "P7 が処刑されました。" in text
    assert "🗳 投票結果:" in text
    # Sort rule: primary key = -count, secondary = target_seat_no (棄権 → +inf).
    # So within count=2, P8 precedes 棄権; overall: P7(4) → P8(2) → 棄権(2) → P9(1).
    p7_idx = text.index("・P7 (4票): P1、P2、P3、P4")
    p8_idx = text.index("・P8 (2票): P5、P6")
    abstain_idx = text.index("・棄権 (2票): P8、P9")
    p9_idx = text.index("・P9 (1票): P7")
    assert p7_idx < p8_idx < abstain_idx < p9_idx


def test_runoff_start_log_includes_initial_round_tally() -> None:
    """Tied initial vote → RUNOFF_START log carries the initial-round breakdown."""
    game = _game(day=1)
    players = _players(STANDARD_ROLES)
    seats = _seats()
    votes = [
        _v(1, 7),
        _v(2, 7),
        _v(3, 8),
        _v(4, 8),
        _v(5, 9),
        _v(6, 9),
        _v(7, 9),
        _v(8, 7),
        _v(9, 1),
    ]
    t = plan_day_vote_resolve(game, players, seats, votes, force_skip=False, now_epoch=1000)
    runoff_log = next(log for log in t.public_logs if log.kind == "RUNOFF_START")
    assert "同票のため決選投票に移ります。" in runoff_log.text
    assert "🗳 投票結果:" in runoff_log.text
    # P7 and P9 tied at 3 votes each.
    assert "・P7 (3票):" in runoff_log.text
    assert "・P9 (3票):" in runoff_log.text


def test_all_abstain_no_execution_log_includes_tally() -> None:
    game = _game(day=1)
    players = _players(STANDARD_ROLES)
    seats = _seats()
    votes = [_v(i, None) for i in range(1, 10)]
    t = plan_day_vote_resolve(game, players, seats, votes, force_skip=False, now_epoch=1000)
    no_exec = next(log for log in t.public_logs if log.kind == "NO_EXECUTION")
    assert "🗳 投票結果:" in no_exec.text
    assert "・棄権 (9票): P1、P2、P3、P4、P5、P6、P7、P8、P9" in no_exec.text
