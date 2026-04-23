"""Shared helpers for figuring out which seats still owe a submission.

Used by `recovery_service._derive_pending` (to build a `PendingDecision` when a
deadline expired before the bot could resolve it) and by
`game_service.resend_pending_dms` (to re-send DM UIs to the exact set of
players whose input we're still missing after a restart or `/wolf extend`).
Keeping this logic in one place guarantees the two views stay consistent —
whatever recovery believes is "missing" is exactly what the resend logic DMs.
"""

from __future__ import annotations

from collections.abc import Sequence

from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import (
    Game,
    PendingDecision,
    PendingSubmission,
    Player,
)
from wolfbot.persistence.sqlite_repo import SqliteRepo


async def missing_submitters(
    repo: SqliteRepo,
    game: Game,
    players: Sequence[Player],
) -> dict[SubmissionType, tuple[int, ...]]:
    """Return {SubmissionType: (seat_no, ...)} of seats that haven't submitted yet.

    Only non-empty buckets are included. Returns an empty dict for phases
    that do not require user submissions (LOBBY, SETUP, NIGHT_0, DAY_DISCUSSION,
    WAITING_HOST_DECISION, GAME_OVER).
    """
    alive_seats = {p.seat_no for p in players if p.alive}

    if game.phase is Phase.DAY_VOTE or game.phase is Phase.DAY_RUNOFF:
        round_ = 0 if game.phase is Phase.DAY_VOTE else 1
        votes = await repo.load_votes(game.id, game.day_number, round_=round_)
        voted = {v.voter_seat for v in votes}
        missing = tuple(sorted(alive_seats - voted))
        if not missing:
            return {}
        kind = SubmissionType.VOTE if game.phase is Phase.DAY_VOTE else SubmissionType.RUNOFF_VOTE
        return {kind: missing}

    if game.phase is Phase.NIGHT:
        actions = await repo.load_night_actions(game.id, game.day_number)
        submitted_by_kind: dict[SubmissionType, set[int]] = {
            SubmissionType.WOLF_ATTACK: set(),
            SubmissionType.SEER_DIVINE: set(),
            SubmissionType.KNIGHT_GUARD: set(),
        }
        for a in actions:
            if a.kind in submitted_by_kind:
                submitted_by_kind[a.kind].add(a.actor_seat)

        expected_pairs: list[tuple[SubmissionType, Role]] = [
            (SubmissionType.WOLF_ATTACK, Role.WEREWOLF),
            (SubmissionType.SEER_DIVINE, Role.SEER),
        ]
        # Knight only acts starting night 1 (state_machine gate).
        if game.day_number >= 1:
            expected_pairs.append((SubmissionType.KNIGHT_GUARD, Role.KNIGHT))

        result: dict[SubmissionType, tuple[int, ...]] = {}
        for kind, required_role in expected_pairs:
            expected = {p.seat_no for p in players if p.alive and p.role is required_role}
            missing = tuple(sorted(expected - submitted_by_kind[kind]))
            if missing:
                result[kind] = missing
        return result

    return {}


async def derive_pending(
    repo: SqliteRepo,
    game: Game,
    players: Sequence[Player],
    now: int,
) -> PendingDecision:
    """Build a full `PendingDecision` for a submission phase whose deadline expired.

    Delegates the set-diff work to `missing_submitters` so the view is
    consistent with `resend_pending_dms`.
    """
    missing = await missing_submitters(repo, game, players)

    if game.phase is Phase.DAY_VOTE or game.phase is Phase.DAY_RUNOFF:
        kind = SubmissionType.VOTE if game.phase is Phase.DAY_VOTE else SubmissionType.RUNOFF_VOTE
        missing_seats = missing.get(kind, ())
        return PendingDecision(
            game_id=game.id,
            phase=game.phase,
            day=game.day_number,
            required_submission=kind,
            missing_seats=missing_seats,
            submissions=(PendingSubmission(submission_type=kind, missing_seats=missing_seats),),
            created_at=now,
        )

    if game.phase is Phase.NIGHT:
        subs = [
            PendingSubmission(submission_type=kind, missing_seats=seats)
            for kind, seats in missing.items()
        ]
        if not subs:
            # Deadline fired with all submissions already in (race condition);
            # park on WOLF_ATTACK as the nominal primary with an empty seat list.
            subs = [PendingSubmission(submission_type=SubmissionType.WOLF_ATTACK, missing_seats=())]
        primary = subs[0]
        return PendingDecision(
            game_id=game.id,
            phase=Phase.NIGHT,
            day=game.day_number,
            required_submission=primary.submission_type,
            missing_seats=primary.missing_seats,
            submissions=tuple(subs),
            created_at=now,
        )

    # Fallback: no derivable pending for this phase.
    return PendingDecision(
        game_id=game.id,
        phase=game.phase,
        day=game.day_number,
        required_submission=SubmissionType.VOTE,
        missing_seats=(),
        submissions=(),
        created_at=now,
    )
