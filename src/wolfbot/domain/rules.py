"""Pure rules for 9-player werewolf.

No I/O, no global clocks. Random.Random is always an explicit parameter so tests can seed it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from random import Random

from wolfbot.domain.enums import (
    ROLE_DISTRIBUTION,
    VILLAGE_SIZE,
    Faction,
    Role,
)
from wolfbot.domain.models import AttackResult, NightAction, Player, Seat, Vote, VoteOutcome


def assign_roles(seats: Sequence[Seat], rng: Random) -> dict[int, Role]:
    """Shuffle the 9 roles (2/1/1/1/1/3) and bind them to seats by seat_no.

    Raises ValueError if len(seats) != 9.
    """
    if len(seats) != VILLAGE_SIZE:
        raise ValueError(f"Village size must be {VILLAGE_SIZE}; got {len(seats)}")
    pool: list[Role] = []
    for role, count in ROLE_DISTRIBUTION.items():
        pool.extend([role] * count)
    assert len(pool) == VILLAGE_SIZE, "ROLE_DISTRIBUTION must sum to 9"
    rng.shuffle(pool)
    return {seat.seat_no: pool[i] for i, seat in enumerate(seats)}


def day_discussion_duration(day_number: int) -> int:
    """Seconds for the discussion phase. Defaults: day 1: 300, day 2:
    240, day 3+: 180.

    Reads the live :class:`PhaseDurations` singleton — runtime-mutable
    so a future ``/wolf settings`` slash command can change durations
    without restarting Master. See :mod:`wolfbot.domain.durations` for
    the full singleton contract.
    """
    from wolfbot.domain.durations import current_phase_durations

    return current_phase_durations().discussion_for_day(day_number)


def legal_attack_targets(players: Sequence[Player], actor_seat: int) -> list[int]:
    return [
        p.seat_no
        for p in players
        if p.alive and p.seat_no != actor_seat and p.role is not Role.WEREWOLF
    ]


def legal_divine_targets(players: Sequence[Player], seer_seat: int) -> list[int]:
    return [p.seat_no for p in players if p.alive and p.seat_no != seer_seat]


def legal_guard_targets(
    players: Sequence[Player],
    knight_seat: int,
    previous_guard_seat: int | None,
) -> list[int]:
    return [
        p.seat_no
        for p in players
        if p.alive and p.seat_no != knight_seat and p.seat_no != previous_guard_seat
    ]


def previous_guard_seat_for_night(
    prev: tuple[int, int | None, int | None] | None,
    current_day: int,
) -> int | None:
    """Return the seat the knight must not re-guard this night, or None.

    The `previous_guard` row stores `last_guard_day = next_day at recording
    time` — i.e. a guard committed during NIGHT of day N is written with
    `last_guard_day == N+1`. When planning / validating a guard on NIGHT of
    day D we only forbid re-targeting when `last_guard_day == D`; a row with
    an older `last_guard_day` means there was an intervening unresolved night
    (e.g. a host force-skip with no knight submission) and the restriction
    has lapsed. Also returns None if `prev` has no seat recorded yet.
    """
    if prev is None:
        return None
    _, last_seat, last_day = prev
    if last_seat is None or last_day != current_day:
        return None
    return last_seat


def random_white_target(players: Sequence[Player], seer_seat: int, rng: Random) -> int:
    """Pick a NIGHT_0 random-white target: alive, not seer, not a werewolf."""
    pool = [
        p.seat_no
        for p in players
        if p.alive and p.seat_no != seer_seat and p.role is not Role.WEREWOLF
    ]
    if not pool:
        raise RuntimeError("No legal NIGHT_0 white target available")
    return rng.choice(pool)


def compute_vote_result(
    votes: Sequence[Vote],
    alive_seats: set[int],
    candidate_seats: set[int] | None = None,
) -> VoteOutcome:
    """Count submitted votes (target_seat is None means abstention).

    - Only voters in `alive_seats` count.
    - If `candidate_seats` is provided (runoff), only votes whose target is in it count.
    - If no valid votes at all → VoteOutcome(executed=None, tied=()).
    - Single-max → executed.
    - Multi-max → tied=tuple(sorted(tied_seats)).
    """
    tallies: Counter[int] = Counter()
    for v in votes:
        if v.voter_seat not in alive_seats:
            continue
        if v.target_seat is None:
            continue
        if v.target_seat == v.voter_seat:  # self-vote is illegal, silently skip
            continue
        if candidate_seats is not None and v.target_seat not in candidate_seats:
            continue
        tallies[v.target_seat] += 1

    if not tallies:
        return VoteOutcome(executed=None, tied=())
    top = max(tallies.values())
    tied = sorted(s for s, c in tallies.items() if c == top)
    if len(tied) == 1:
        return VoteOutcome(executed=tied[0], tied=())
    return VoteOutcome(executed=None, tied=tuple(tied))


def resolve_wolf_attack(
    actions: Sequence[NightAction],
    alive_wolf_seats: Sequence[int],
    force_skip: bool,
    human_wolf_seats: Sequence[int] = (),
    rng: Random | None = None,
) -> AttackResult:
    """Determine the night's attack target per spec.

    - Solo wolf: their pick (or None if missing).
    - Two wolves: both submit same target → attack; otherwise split.
    - With force_skip=False, any missing wolf → AttackResult.missing populated so the
      caller can pause into WAITING_HOST_DECISION without committing a target.
    - The `missing` tuple is always reported (even with force_skip) so logs can name
      who didn't submit.
    - When 2 wolves disagree, both submitted, and exactly one of them is a human
      seat (per `human_wolf_seats`), the human's target wins (no split).
    - When 2 wolves disagree, both submitted, both are LLMs, and ``rng`` is
      provided, randomly pick one of the two concrete picks as the resolved
      attack target. This unblocks games that previously parked in
      ``WAITING_HOST_DECISION`` whenever the wolf-chat coordination LLM
      failed to make the two NPCs converge (observed in game
      ``98e5a083b5ff`` day 2: SQ→コメット, ユリコ→セツ → host paused
      indefinitely with no human able to break the tie).
    - When ``rng`` is None and no human-wolf priority applies, the legacy
      ``split=True`` shape is returned so unit tests pinning the old
      behaviour keep passing.
    - If both wolves' picks are ``None`` (= both abstained), the result
      stays "no attack" rather than synthesising a target.
    """
    alive = list(alive_wolf_seats)
    if not alive:
        return AttackResult()

    picks: dict[int, int | None] = {
        a.actor_seat: a.target_seat for a in actions if a.actor_seat in alive
    }
    missing = tuple(seat for seat in alive if seat not in picks)

    if missing and not force_skip:
        return AttackResult(missing=missing)

    if len(alive) == 1:
        wolf = alive[0]
        return AttackResult(target_seat=picks.get(wolf), missing=missing)

    targets = [picks.get(w) for w in alive]
    if targets[0] is None and targets[1] is None:
        # Both wolves explicitly abstained — that's a unanimous "no
        # attack" rather than a split. The legacy code reported
        # split=True here because the equality check bailed out on
        # ``None``; we promote it to a clean no-attack result so the
        # caller doesn't pause the night chasing a phantom split.
        return AttackResult(target_seat=None, missing=missing)
    if targets[0] is not None and targets[0] == targets[1]:
        return AttackResult(target_seat=targets[0], missing=missing)
    # Targets differ. Check human-wolf priority: only when exactly 2 alive wolves,
    # neither is missing, and exactly one of them is human.
    if len(alive) == 2 and not missing:
        humans_alive = {s for s in human_wolf_seats if s in alive}
        if len(humans_alive) == 1:
            human_seat = next(iter(humans_alive))
            human_target = picks.get(human_seat)
            if human_target is not None:
                return AttackResult(target_seat=human_target, missing=missing)
    # All-LLM split (or unfilled human-wolf seats with both LLM picks
    # diverging). With an RNG seeded by the caller we resolve the split
    # by randomly picking one of the two concrete picks; without one we
    # fall back to the legacy split=True for callers (mostly tests) that
    # haven't started threading an RNG.
    concrete_targets = [t for t in targets if t is not None]
    if rng is not None and concrete_targets:
        chosen = rng.choice(concrete_targets)
        return AttackResult(target_seat=chosen, missing=missing)
    return AttackResult(split=True, missing=missing)


def is_detected_as_wolf(role: Role | None) -> bool:
    """Seer and medium only see `Role.WEREWOLF` as wolf. Madmen appear white."""
    return role is Role.WEREWOLF


def medium_detection(executed_player: Player | None) -> bool | None:
    """True iff the executed player was a real werewolf. None when no execution today."""
    if executed_player is None or executed_player.role is None:
        return None
    return is_detected_as_wolf(executed_player.role)


def check_victory(players: Sequence[Player]) -> Faction | None:
    """Spec win rules.

    - Village wins: alive werewolves == 0.
    - Werewolves win: alive werewolves >= alive non-werewolves.
      (Madman is counted as non-werewolf for this arithmetic per spec "生存非人狼人数".)
    - Otherwise None.
    """
    alive = [p for p in players if p.alive]
    alive_wolves = sum(1 for p in alive if p.role is Role.WEREWOLF)
    alive_non_wolves = len(alive) - alive_wolves
    if alive_wolves == 0:
        return Faction.VILLAGE
    if alive_wolves >= alive_non_wolves:
        return Faction.WEREWOLVES
    return None


def alive_werewolves(players: Sequence[Player]) -> list[int]:
    return [p.seat_no for p in players if p.alive and p.role is Role.WEREWOLF]


def seat_of_role(players: Sequence[Player], role: Role) -> int | None:
    """Find the (unique) seat assigned to role. Returns None if dead/missing."""
    for p in players:
        if p.role is role:
            return p.seat_no
    return None


def alive_seat_of_role(players: Sequence[Player], role: Role) -> int | None:
    for p in players:
        if p.role is role and p.alive:
            return p.seat_no
    return None
