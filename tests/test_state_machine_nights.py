"""State machine: plan_night_resolve — strict 10-step order + edge cases."""

from __future__ import annotations

from wolfbot.domain.enums import (
    DeathCause,
    Faction,
    Phase,
    Role,
    SubmissionType,
)
from wolfbot.domain.models import Game, NightAction, Player, Seat
from wolfbot.domain.state_machine import plan_night_resolve


def _game(phase: Phase = Phase.NIGHT, day: int = 1) -> Game:
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


def _players(
    alive: list[bool] | None = None,
    executed_today: int | None = None,
    day: int = 1,
) -> list[Player]:
    ps: list[Player] = []
    for i, r in enumerate(STANDARD_ROLES, start=1):
        live = True if alive is None else alive[i - 1]
        player = Player(seat_no=i, role=r, alive=live)
        if executed_today == i:
            player.alive = False
            player.death_cause = DeathCause.EXECUTION
            player.death_day = day
        ps.append(player)
    return ps


def _seats() -> list[Seat]:
    return [
        Seat(
            seat_no=i, display_name=f"P{i}", discord_user_id=f"u{i}", is_llm=False, persona_key=None
        )
        for i in range(1, 10)
    ]


def _act(seat: int, kind: SubmissionType, target: int | None, day: int = 1) -> NightAction:
    return NightAction(
        game_id="g1",
        day=day,
        actor_seat=seat,
        kind=kind,
        target_seat=target,
        submitted_at=0,
    )


# ---------------------------------------------------------------- guard == attack
def test_guard_equals_attack_results_in_no_death() -> None:
    game = _game(day=1)
    players = _players()
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7),
        _act(2, SubmissionType.WOLF_ATTACK, 7),
        _act(4, SubmissionType.SEER_DIVINE, 8),
        _act(6, SubmissionType.KNIGHT_GUARD, 7),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    assert t.player_updates == ()
    assert t.newly_dead_seats == ()
    assert t.next_phase is Phase.DAY_DISCUSSION
    assert t.next_day == 2
    assert t.morning_text is not None
    assert "平和" in t.morning_text


def test_attack_succeeds_when_guard_differs() -> None:
    game = _game(day=1)
    players = _players()
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7),
        _act(2, SubmissionType.WOLF_ATTACK, 7),
        _act(4, SubmissionType.SEER_DIVINE, 8),
        _act(6, SubmissionType.KNIGHT_GUARD, 8),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    assert t.newly_dead_seats == (7,)
    upd = next(u for u in t.player_updates if u.seat_no == 7)
    assert upd.alive is False
    assert upd.death_cause is DeathCause.ATTACK


def test_plan_night_resolve_skips_dead_attack_target() -> None:
    """If attack target is already dead, death_day must not be overwritten."""
    game = _game(day=2)
    # seat 7 was killed on day 1 (execution)
    alive = [True, True, True, True, True, True, False, True, True]
    players = _players(alive=alive, executed_today=7, day=1)
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7, day=2),
        _act(2, SubmissionType.WOLF_ATTACK, 7, day=2),
        _act(4, SubmissionType.SEER_DIVINE, 8, day=2),
        _act(6, SubmissionType.KNIGHT_GUARD, 8, day=2),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    # No new death, no player_update for dead seat — death_day of seat 7 preserved (=1)
    assert t.newly_dead_seats == ()
    assert all(u.seat_no != 7 for u in t.player_updates)
    assert t.morning_text is not None
    assert "平和" in t.morning_text


def test_morning_announce_does_not_reveal_role() -> None:
    game = _game(day=1)
    players = _players()
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 4),  # seer attacked
        _act(2, SubmissionType.WOLF_ATTACK, 4),
        _act(4, SubmissionType.SEER_DIVINE, 1),
        _act(6, SubmissionType.KNIGHT_GUARD, 3),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    assert t.newly_dead_seats == (4,)
    assert t.morning_text is not None
    # Morning should NOT mention 占い師 (seer role)
    for role_ja in ["占い師", "人狼", "狂人", "霊媒師", "騎士", "村人"]:
        assert role_ja not in t.morning_text, (
            f"morning_text leaked role: {role_ja} -- {t.morning_text}"
        )


# ---------------------------------------------------------------- medium
def test_medium_private_log_returns_faction_not_role() -> None:
    game = _game(day=1)
    players = _players(executed_today=1, day=1)  # seat 1 (wolf) was executed today
    seats = _seats()
    actions = [
        # wolf 1 is dead (executed); wolf 2 alive
        _act(2, SubmissionType.WOLF_ATTACK, 7),
        _act(4, SubmissionType.SEER_DIVINE, 3),
        _act(6, SubmissionType.KNIGHT_GUARD, 7),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    medium_logs = [lg for lg in t.private_logs if lg.kind == "MEDIUM_RESULT"]
    assert len(medium_logs) == 1
    medium = medium_logs[0]
    assert medium.audience_seat == 5
    # Medium should receive the faction label, not the exact role name
    assert "人狼陣営" in medium.text
    for role_ja in ["占い師", "狂人", "霊媒師", "騎士"]:
        assert role_ja not in medium.text


def test_medium_no_execution_reports_none() -> None:
    game = _game(day=1)
    players = _players()  # no executed today
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7),
        _act(2, SubmissionType.WOLF_ATTACK, 7),
        _act(4, SubmissionType.SEER_DIVINE, 3),
        _act(6, SubmissionType.KNIGHT_GUARD, 8),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    medium_logs = [lg for lg in t.private_logs if lg.kind == "MEDIUM_RESULT"]
    assert len(medium_logs) == 1
    assert "処刑なし" in medium_logs[0].text


# ---------------------------------------------------------------- seer
def test_seer_gets_result_even_if_target_dies_tonight() -> None:
    game = _game(day=1)
    players = _players()
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 8),
        _act(2, SubmissionType.WOLF_ATTACK, 8),
        _act(4, SubmissionType.SEER_DIVINE, 8),  # targets victim of tonight
        _act(6, SubmissionType.KNIGHT_GUARD, 3),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    seer_logs = [lg for lg in t.private_logs if lg.kind == "SEER_RESULT"]
    assert len(seer_logs) == 1
    # Target 8 is villager → village faction
    assert "村人陣営" in seer_logs[0].text


def test_seer_on_wolf_returns_wolf_faction() -> None:
    game = _game(day=1)
    players = _players()
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7),
        _act(2, SubmissionType.WOLF_ATTACK, 7),
        _act(4, SubmissionType.SEER_DIVINE, 1),  # peek wolf
        _act(6, SubmissionType.KNIGHT_GUARD, 3),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    seer_logs = [lg for lg in t.private_logs if lg.kind == "SEER_RESULT"]
    assert "人狼陣営" in seer_logs[0].text


# ---------------------------------------------------------------- missing / pause
def test_wolf_split_without_force_skip_pauses() -> None:
    game = _game(day=1)
    players = _players()
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7),
        _act(2, SubmissionType.WOLF_ATTACK, 8),  # split
        _act(4, SubmissionType.SEER_DIVINE, 3),
        _act(6, SubmissionType.KNIGHT_GUARD, 3),
    ]
    # attack.missing is empty but split=True. My spec says split pauses only if no force_skip
    # but resolve_wolf_attack only puts "missing" in missing — for split with no force_skip,
    # the plan_night_resolve currently requires missing to pause. We need to verify this.
    # With force_skip=False and both wolves submitted different targets, attack.split=True
    # but attack.missing=(). plan_night_resolve will NOT pause. Let's check the logic.
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    # Per spec: "1 対 1 で割れた場合、締切時点では未確定のままとし、WAITING_HOST_DECISION に遷移"
    # So split WITHOUT force_skip must pause.
    assert t.next_phase is Phase.WAITING_HOST_DECISION
    assert t.requires_host_decision is True


def test_wolf_split_records_unresolved_seats_not_missing() -> None:
    """Split wolves have submitted — they must be classified as `unresolved_seats`
    (so recovery and /wolf extend can distinguish them from truly missing players)."""
    game = _game(day=1)
    players = _players()
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7),
        _act(2, SubmissionType.WOLF_ATTACK, 8),  # split
        _act(4, SubmissionType.SEER_DIVINE, 3),
        _act(6, SubmissionType.KNIGHT_GUARD, 3),
    ]
    t = plan_night_resolve(
        game, players, seats, actions, previous_guard_seat=None, force_skip=False, now_epoch=1000
    )
    assert t.pending is not None
    wolf_sub = next(
        s for s in t.pending.submissions if s.submission_type is SubmissionType.WOLF_ATTACK
    )
    assert wolf_sub.missing_seats == ()
    assert wolf_sub.unresolved_seats == (1, 2)
    # The legacy summary `missing_seats` still lists the wolves so existing UI surfaces
    # continue to report "wolves need action" in a single field.
    assert set(t.pending.missing_seats) == {1, 2}


def test_wolf_missing_submission_pauses() -> None:
    game = _game(day=1)
    players = _players()
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7),
        # wolf 2 didn't submit
        _act(4, SubmissionType.SEER_DIVINE, 3),
        _act(6, SubmissionType.KNIGHT_GUARD, 8),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    assert t.next_phase is Phase.WAITING_HOST_DECISION
    assert t.pending is not None
    assert 2 in t.pending.missing_seats


def test_wolf_missing_with_force_skip_fails_attack() -> None:
    game = _game(day=1)
    players = _players()
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7),
        _act(4, SubmissionType.SEER_DIVINE, 3),
        _act(6, SubmissionType.KNIGHT_GUARD, 8),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=True,
        now_epoch=1000,
    )
    # force_skip makes missing treat wolf 2 as no-action → split → failed attack
    assert t.newly_dead_seats == ()
    assert t.next_phase is Phase.DAY_DISCUSSION
    assert t.next_day == 2


def test_seer_missing_pauses() -> None:
    game = _game(day=1)
    players = _players()
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7),
        _act(2, SubmissionType.WOLF_ATTACK, 7),
        _act(6, SubmissionType.KNIGHT_GUARD, 8),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    assert t.next_phase is Phase.WAITING_HOST_DECISION
    assert 4 in (t.pending.missing_seats if t.pending else ())


def test_knight_missing_pauses() -> None:
    game = _game(day=1)
    players = _players()
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7),
        _act(2, SubmissionType.WOLF_ATTACK, 7),
        _act(4, SubmissionType.SEER_DIVINE, 3),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    assert t.next_phase is Phase.WAITING_HOST_DECISION
    assert 6 in (t.pending.missing_seats if t.pending else ())


# ---------------------------------------------------------------- victory
def test_attack_killing_enough_non_wolves_triggers_wolf_victory() -> None:
    # Late game: only 2 wolves (1, 2), 1 seer (4), 1 villager (7) alive. Attack kills 7 →
    # after attack: 2 wolves vs 1 non-wolf → wolves win.
    game = _game(day=3)
    alive = [True, True, False, True, False, False, True, False, False]
    players = _players(alive=alive)
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7),
        _act(2, SubmissionType.WOLF_ATTACK, 7),
        _act(4, SubmissionType.SEER_DIVINE, 1),
        # knight is dead; no knight action expected
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    assert t.next_phase is Phase.GAME_OVER
    assert t.victory is Faction.WEREWOLVES


# ---------------------------------------------------------------- record_guard
def test_record_guard_persists_knight_choice() -> None:
    game = _game(day=1)
    players = _players()
    seats = _seats()
    actions = [
        _act(1, SubmissionType.WOLF_ATTACK, 7),
        _act(2, SubmissionType.WOLF_ATTACK, 7),
        _act(4, SubmissionType.SEER_DIVINE, 3),
        _act(6, SubmissionType.KNIGHT_GUARD, 8),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    assert t.record_guard == (6, 8)


def test_resolve_night_strict_order_medium_then_seer_then_morning() -> None:
    """Spec: 10-step order. Private logs are in (medium, seer) order;
    morning text is emitted after resolution."""
    game = _game(day=1)
    players = _players(executed_today=1, day=1)  # wolf 1 executed today
    seats = _seats()
    actions = [
        _act(2, SubmissionType.WOLF_ATTACK, 7),
        _act(4, SubmissionType.SEER_DIVINE, 3),
        _act(6, SubmissionType.KNIGHT_GUARD, 8),
    ]
    t = plan_night_resolve(
        game,
        players,
        seats,
        actions,
        previous_guard_seat=None,
        force_skip=False,
        now_epoch=1000,
    )
    # Private logs should include both MEDIUM_RESULT and SEER_RESULT,
    # with MEDIUM coming BEFORE SEER (step 1 before step 2).
    kinds = [lg.kind for lg in t.private_logs]
    assert kinds == ["MEDIUM_RESULT", "SEER_RESULT"]
    # morning text exists and references either the victim or peaceful
    assert t.morning_text is not None


def test_llm_shortfall_padding_count() -> None:
    """Given N humans, pick_personas(9-N) returns exactly the shortfall."""
    import random as _r

    from wolfbot.llm.personas import pick_personas

    rng = _r.Random(0)
    for n in range(0, 10):
        picks = pick_personas(9 - n, rng)
        assert len(picks) == 9 - n
        assert len({p.key for p in picks}) == 9 - n
