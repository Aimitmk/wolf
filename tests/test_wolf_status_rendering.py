"""Tests for `render_pending_host_lines`, the /wolf status embed helper.

Regression: split wolf attacks populate `unresolved_seats` with empty
`missing_seats`, which the old /wolf status code hid. This test locks in that
both lists are surfaced independently.
"""

from __future__ import annotations

from wolfbot.domain.enums import Phase, SubmissionType
from wolfbot.domain.models import PendingDecision, PendingSubmission
from wolfbot.services.discord_service import render_pending_host_lines


def _pending(
    *, phase: Phase, subs: tuple[PendingSubmission, ...]
) -> PendingDecision:
    return PendingDecision(
        game_id="g",
        phase=phase,
        day=1,
        required_submission=SubmissionType.WOLF_ATTACK,
        missing_seats=tuple(sorted({s for sub in subs for s in sub.missing_seats})),
        submissions=subs,
        created_at=0,
    )


def test_render_unresolved_wolf_split_is_visible() -> None:
    """2 狼襲撃割れ: missing=(), unresolved=(1,2) → 「再提出待ち」行が出る。"""
    seat_name = {1: "Alice", 2: "Bob", 3: "Carol"}
    pending = _pending(
        phase=Phase.NIGHT,
        subs=(
            PendingSubmission(
                submission_type=SubmissionType.WOLF_ATTACK,
                missing_seats=(),
                unresolved_seats=(1, 2),
            ),
        ),
    )

    lines = render_pending_host_lines(pending, seat_name)

    assert lines == [
        "`WOLF_ATTACK` 再提出待ち(意見が割れました): Alice、Bob",
    ]


def test_render_missing_only() -> None:
    seat_name = {1: "Alice", 5: "Eve"}
    pending = _pending(
        phase=Phase.DAY_VOTE,
        subs=(
            PendingSubmission(
                submission_type=SubmissionType.VOTE,
                missing_seats=(1, 5),
                unresolved_seats=(),
            ),
        ),
    )

    lines = render_pending_host_lines(pending, seat_name)

    assert lines == ["`VOTE` 未提出: Alice、Eve"]


def test_render_missing_and_unresolved_both_appear() -> None:
    """Night with seer missing and wolves split: both buckets surface."""
    seat_name = {1: "Alice", 2: "Bob", 4: "Dave"}
    pending = _pending(
        phase=Phase.NIGHT,
        subs=(
            PendingSubmission(
                submission_type=SubmissionType.WOLF_ATTACK,
                missing_seats=(),
                unresolved_seats=(1, 2),
            ),
            PendingSubmission(
                submission_type=SubmissionType.SEER_DIVINE,
                missing_seats=(4,),
                unresolved_seats=(),
            ),
        ),
    )

    lines = render_pending_host_lines(pending, seat_name)

    assert lines == [
        "`WOLF_ATTACK` 再提出待ち(意見が割れました): Alice、Bob",
        "`SEER_DIVINE` 未提出: Dave",
    ]


def test_render_empty_when_nothing_pending() -> None:
    pending = _pending(phase=Phase.DAY_VOTE, subs=())
    assert render_pending_host_lines(pending, {}) == []


def test_render_falls_back_to_seat_number_for_unknown_seats() -> None:
    """If seat_name is missing an entry, render the seat_no as string."""
    pending = _pending(
        phase=Phase.NIGHT,
        subs=(
            PendingSubmission(
                submission_type=SubmissionType.WOLF_ATTACK,
                missing_seats=(),
                unresolved_seats=(1, 2),
            ),
        ),
    )

    lines = render_pending_host_lines(pending, {1: "Alice"})

    assert lines == ["`WOLF_ATTACK` 再提出待ち(意見が割れました): Alice、2"]
