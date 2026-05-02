"""State machine: phase ordering and basic transitions."""

from __future__ import annotations

import random

from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Player, Seat
from wolfbot.domain.state_machine import (
    VOTE_DURATION,
    plan_day_discussion_to_vote,
    plan_night0,
    plan_setup,
)


def _game(phase: Phase = Phase.LOBBY, day: int = 0) -> Game:
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


def _nine_seats() -> list[Seat]:
    out: list[Seat] = []
    for i in range(1, 10):
        out.append(
            Seat(
                seat_no=i,
                display_name=f"P{i}",
                discord_user_id=f"u{i}",
                is_llm=False,
                persona_key=None,
            )
        )
    return out


def _players_from(seats: list[Seat], role_updates: dict[int, Role]) -> list[Player]:
    return [Player(seat_no=s.seat_no, role=role_updates.get(s.seat_no), alive=True) for s in seats]


def test_plan_setup_transitions_to_night0_with_roles() -> None:
    game = _game()
    seats = _nine_seats()
    rng = random.Random(0)
    t = plan_setup(game, seats, rng, now_epoch=1000)
    assert t.next_phase is Phase.NIGHT_0
    assert t.next_day == 0
    assert t.new_deadline_epoch is None  # transient
    # All 9 seats get a role assignment
    assigned = {u.seat_no: u.role for u in t.player_updates}
    assert set(assigned.keys()) == {s.seat_no for s in seats}
    assert all(role is not None for role in assigned.values())


def test_plan_night0_transitions_to_day1_with_deadline() -> None:
    game = _game(phase=Phase.NIGHT_0)
    seats = _nine_seats()
    rng = random.Random(0)
    # Explicit role layout for determinism
    roles = {
        1: Role.WEREWOLF,
        2: Role.WEREWOLF,
        3: Role.MADMAN,
        4: Role.SEER,
        5: Role.MEDIUM,
        6: Role.KNIGHT,
        7: Role.VILLAGER,
        8: Role.VILLAGER,
        9: Role.VILLAGER,
    }
    players = _players_from(seats, roles)
    t = plan_night0(game, players, seats, rng, now_epoch=2000)
    assert t.next_phase is Phase.DAY_DISCUSSION
    assert t.next_day == 1
    assert t.new_deadline_epoch == 2000 + 300
    # 9 role notices + 1 seer random white + 2 wolf partner notices = 12 private logs
    assert len(t.private_logs) == 12
    kinds = sorted({lg.kind for lg in t.private_logs})
    assert "ROLE_NOTICE" in kinds
    assert "SEER_RESULT_NIGHT0" in kinds
    assert "WOLF_PARTNER" in kinds
    # The day-1-start PHASE_CHANGE log must be tagged day=1, not the
    # game's current day_number (=0 at NIGHT_0). Same bug class as
    # plan_night_resolve's MORNING/PHASE_CHANGE — without this tag the
    # viewer puts day-1's discussion-open in the day-0 SETUP bucket
    # and day-1 DAY_DISCUSSION starts off without its phase-boundary
    # log.
    day1_start = next(
        log
        for log in t.public_logs
        if log.kind == "PHASE_CHANGE" and "1 日目の議論を開始" in log.text
    )
    assert day1_start.day == 1


def test_plan_night0_random_white_is_non_wolf_non_self() -> None:
    game = _game(phase=Phase.NIGHT_0)
    seats = _nine_seats()
    roles = {
        1: Role.WEREWOLF,
        2: Role.WEREWOLF,
        3: Role.MADMAN,
        4: Role.SEER,
        5: Role.MEDIUM,
        6: Role.KNIGHT,
        7: Role.VILLAGER,
        8: Role.VILLAGER,
        9: Role.VILLAGER,
    }
    players = _players_from(seats, roles)
    saw_madman = False
    for seed in range(50):
        rng = random.Random(seed)
        t = plan_night0(game, players, seats, rng, now_epoch=0)
        white_log = next(lg for lg in t.private_logs if lg.kind == "SEER_RESULT_NIGHT0")
        # audience must be seer
        assert white_log.audience_seat == 4
        # Text references one of the non-wolf non-seer seats (3, 5, 6, 7, 8, 9)
        # (We don't parse the name — just check that the target is not the seer itself)
        assert "P4" not in white_log.text
        # Binary framing — seer learns only "not werewolf", never a faction label.
        assert "人狼ではありません" in white_log.text
        assert "村人陣営" not in white_log.text
        # Madman (seat 3 → display name "P3") is a legal white target.
        if "P3" in white_log.text:
            saw_madman = True
    assert saw_madman, "madman should appear in the random-white pool at least once over 50 seeds"


def test_plan_day_discussion_to_vote_sets_vote_deadline() -> None:
    game = _game(phase=Phase.DAY_DISCUSSION, day=1)
    t = plan_day_discussion_to_vote(game, now_epoch=5000)
    assert t.next_phase is Phase.DAY_VOTE
    assert t.next_day == 1
    assert t.new_deadline_epoch == 5000 + VOTE_DURATION


def test_plan_night0_madman_does_not_learn_partner() -> None:
    game = _game(phase=Phase.NIGHT_0)
    seats = _nine_seats()
    roles = {
        1: Role.WEREWOLF,
        2: Role.WEREWOLF,
        3: Role.MADMAN,
        4: Role.SEER,
        5: Role.MEDIUM,
        6: Role.KNIGHT,
        7: Role.VILLAGER,
        8: Role.VILLAGER,
        9: Role.VILLAGER,
    }
    players = _players_from(seats, roles)
    rng = random.Random(0)
    t = plan_night0(game, players, seats, rng, now_epoch=0)
    wolf_partner_logs = [lg for lg in t.private_logs if lg.kind == "WOLF_PARTNER"]
    audience = {lg.audience_seat for lg in wolf_partner_logs}
    # only seats 1 and 2 (the wolves) receive partner info — madman (seat 3) must NOT
    assert audience == {1, 2}
    assert 3 not in audience
