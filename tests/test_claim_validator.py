"""Tests for `wolfbot.master.claim_validator`.

Covers the matrix:
  * real seer: legal / wrong target / wrong color
  * fake seer: legal / day-1 morning 2nd claim / target swap / color flip
  * real medium: legal / wrong target / wrong color
  * fake medium: day-1 morning / no executions yet / target swap / color flip

The validator is pure — no fixtures, no asyncio, no DB.
"""

from __future__ import annotations

from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.ws_messages import ClaimedMediumResult, ClaimedSeerResult
from wolfbot.master.claim_history import (
    ClaimedMediumEntry,
    ClaimedSeerEntry,
    ClaimerHistory,
)
from wolfbot.master.claim_validator import (
    REASON_MEDIUM_DAY1,
    REASON_MEDIUM_FABRICATED_TARGET,
    REASON_MEDIUM_NO_EXECUTION,
    REASON_MEDIUM_TARGET_SWAP,
    REASON_MEDIUM_VERDICT_FLIP,
    REASON_MEDIUM_WRONG_VERDICT,
    REASON_SEER_DAY1_OVERFLOW,
    REASON_SEER_FABRICATED_TARGET,
    REASON_SEER_TARGET_SWAP,
    REASON_SEER_VERDICT_FLIP,
    REASON_SEER_WRONG_VERDICT,
    ActualMediumEvent,
    ActualSeerEvent,
    validate_claim_against_truth,
)

# ─── real seer ────────────────────────────────────────────────────────


def test_real_seer_matching_target_and_color_passes() -> None:
    res = validate_claim_against_truth(
        speaker_role=Role.SEER,
        speaker_seat=5,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=ClaimedSeerResult(target_seat=9, is_wolf=False),
        claimed_medium=None,
        actual_seer_history=[ActualSeerEvent(day=0, target_seat=9, is_wolf=False)],
    )
    assert res.ok, res.feedback


def test_real_seer_fabricated_target_rejected() -> None:
    """Reproduces game ba084ae208cc / 101d9a90ab58: real seer claims
    a target that doesn't appear in their NIGHT_0 random white."""
    res = validate_claim_against_truth(
        speaker_role=Role.SEER,
        speaker_seat=5,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=ClaimedSeerResult(target_seat=4, is_wolf=False),
        claimed_medium=None,
        actual_seer_history=[ActualSeerEvent(day=0, target_seat=9, is_wolf=False)],
    )
    assert not res.ok
    assert res.reason == REASON_SEER_FABRICATED_TARGET
    assert res.feedback is not None
    assert "席4" in res.feedback or "target_seat=4" in res.feedback


def test_real_seer_wrong_color_rejected() -> None:
    res = validate_claim_against_truth(
        speaker_role=Role.SEER,
        speaker_seat=5,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=ClaimedSeerResult(target_seat=9, is_wolf=True),
        claimed_medium=None,
        actual_seer_history=[ActualSeerEvent(day=0, target_seat=9, is_wolf=False)],
    )
    assert not res.ok
    assert res.reason == REASON_SEER_WRONG_VERDICT


def test_real_seer_null_claim_passes() -> None:
    """Utterances that don't announce a new result are always OK."""
    res = validate_claim_against_truth(
        speaker_role=Role.SEER,
        speaker_seat=5,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=None,
        claimed_medium=None,
        actual_seer_history=[ActualSeerEvent(day=0, target_seat=9, is_wolf=False)],
    )
    assert res.ok


# ─── fake seer (wolf / madman / villager bluff) ──────────────────────


def test_fake_seer_first_claim_passes() -> None:
    res = validate_claim_against_truth(
        speaker_role=Role.WEREWOLF,
        speaker_seat=2,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=ClaimedSeerResult(target_seat=4, is_wolf=False),
        claimed_medium=None,
        prior_public_claims=ClaimerHistory(claimer_seat=2),
    )
    assert res.ok


def test_fake_seer_day1_overflow_rejected() -> None:
    """Fake seer who already claimed once on day 1 morning can't issue
    a second claim same morning."""
    prior = ClaimerHistory(
        claimer_seat=2,
        seer_claims=(
            ClaimedSeerEntry(
                day=1,
                target_seat=4,
                target_name="席4",
                is_wolf=False,
                declared_at_event_id="e1",
            ),
        ),
    )
    res = validate_claim_against_truth(
        speaker_role=Role.WEREWOLF,
        speaker_seat=2,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=ClaimedSeerResult(target_seat=5, is_wolf=False),
        claimed_medium=None,
        prior_public_claims=prior,
    )
    assert not res.ok
    assert res.reason == REASON_SEER_DAY1_OVERFLOW


def test_fake_seer_target_swap_same_night_rejected() -> None:
    """Wolf claimed Alice white in turn 1. Tries to claim Bob in turn 2."""
    prior = ClaimerHistory(
        claimer_seat=2,
        seer_claims=(
            ClaimedSeerEntry(
                day=2,
                target_seat=4,
                target_name="席4",
                is_wolf=False,
                declared_at_event_id="e1",
            ),
        ),
    )
    res = validate_claim_against_truth(
        speaker_role=Role.WEREWOLF,
        speaker_seat=2,
        day=2,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=ClaimedSeerResult(target_seat=5, is_wolf=False),
        claimed_medium=None,
        prior_public_claims=prior,
    )
    assert not res.ok
    assert res.reason == REASON_SEER_TARGET_SWAP


def test_fake_seer_verdict_flip_rejected() -> None:
    """Wolf said Alice white, then later flips Alice to black."""
    prior = ClaimerHistory(
        claimer_seat=2,
        seer_claims=(
            ClaimedSeerEntry(
                day=2,
                target_seat=4,
                target_name="席4",
                is_wolf=False,
                declared_at_event_id="e1",
            ),
        ),
    )
    res = validate_claim_against_truth(
        speaker_role=Role.WEREWOLF,
        speaker_seat=2,
        day=2,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=ClaimedSeerResult(target_seat=4, is_wolf=True),
        claimed_medium=None,
        prior_public_claims=prior,
    )
    assert not res.ok
    assert res.reason == REASON_SEER_VERDICT_FLIP


def test_fake_seer_repeat_same_target_color_passes() -> None:
    """Re-citing the same prior claim (same target, same color) is fine —
    the speaker is just referencing what they said yesterday."""
    prior = ClaimerHistory(
        claimer_seat=2,
        seer_claims=(
            ClaimedSeerEntry(
                day=2,
                target_seat=4,
                target_name="席4",
                is_wolf=False,
                declared_at_event_id="e1",
            ),
        ),
    )
    res = validate_claim_against_truth(
        speaker_role=Role.WEREWOLF,
        speaker_seat=2,
        day=2,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=ClaimedSeerResult(target_seat=4, is_wolf=False),
        claimed_medium=None,
        prior_public_claims=prior,
    )
    assert res.ok


def test_fake_seer_new_day_new_claim_passes() -> None:
    """A fake seer can claim a new result on a new day."""
    prior = ClaimerHistory(
        claimer_seat=2,
        seer_claims=(
            ClaimedSeerEntry(
                day=1,
                target_seat=4,
                target_name="席4",
                is_wolf=False,
                declared_at_event_id="e1",
            ),
        ),
    )
    res = validate_claim_against_truth(
        speaker_role=Role.WEREWOLF,
        speaker_seat=2,
        day=2,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=ClaimedSeerResult(target_seat=5, is_wolf=False),
        claimed_medium=None,
        prior_public_claims=prior,
    )
    assert res.ok


# ─── real medium ──────────────────────────────────────────────────────


def test_real_medium_matching_passes() -> None:
    res = validate_claim_against_truth(
        speaker_role=Role.MEDIUM,
        speaker_seat=6,
        day=2,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=None,
        claimed_medium=ClaimedMediumResult(target_seat=7, is_wolf=False),
        actual_medium_history=[
            ActualMediumEvent(day=2, target_seat=7, is_wolf=False),
        ],
        executions_so_far=1,
    )
    assert res.ok


def test_real_medium_fabricated_target_rejected() -> None:
    res = validate_claim_against_truth(
        speaker_role=Role.MEDIUM,
        speaker_seat=6,
        day=2,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=None,
        claimed_medium=ClaimedMediumResult(target_seat=3, is_wolf=False),
        actual_medium_history=[
            ActualMediumEvent(day=2, target_seat=7, is_wolf=False),
        ],
        executions_so_far=1,
    )
    assert not res.ok
    assert res.reason == REASON_MEDIUM_FABRICATED_TARGET


def test_real_medium_wrong_color_rejected() -> None:
    res = validate_claim_against_truth(
        speaker_role=Role.MEDIUM,
        speaker_seat=6,
        day=2,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=None,
        claimed_medium=ClaimedMediumResult(target_seat=7, is_wolf=True),
        actual_medium_history=[
            ActualMediumEvent(day=2, target_seat=7, is_wolf=False),
        ],
        executions_so_far=1,
    )
    assert not res.ok
    assert res.reason == REASON_MEDIUM_WRONG_VERDICT


# ─── fake medium ─────────────────────────────────────────────────────


def test_fake_medium_day1_morning_rejected() -> None:
    """Day-1 morning has no legal medium result — fake medium can't
    invent one without immediately outing themselves."""
    res = validate_claim_against_truth(
        speaker_role=Role.WEREWOLF,
        speaker_seat=2,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=None,
        claimed_medium=ClaimedMediumResult(target_seat=4, is_wolf=False),
        prior_public_claims=ClaimerHistory(claimer_seat=2),
    )
    assert not res.ok
    assert res.reason == REASON_MEDIUM_DAY1


def test_fake_medium_no_execution_yet_rejected() -> None:
    """day 2 morning but executions_so_far == 0 means no execution to
    medium (e.g. day-1 ended without a vote outcome)."""
    res = validate_claim_against_truth(
        speaker_role=Role.WEREWOLF,
        speaker_seat=2,
        day=2,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=None,
        claimed_medium=ClaimedMediumResult(target_seat=4, is_wolf=False),
        prior_public_claims=ClaimerHistory(claimer_seat=2),
        executions_so_far=0,
    )
    assert not res.ok
    assert res.reason == REASON_MEDIUM_NO_EXECUTION


def test_fake_medium_target_swap_rejected() -> None:
    prior = ClaimerHistory(
        claimer_seat=2,
        medium_claims=(
            ClaimedMediumEntry(
                day=2,
                target_seat=4,
                target_name="席4",
                is_wolf=False,
                declared_at_event_id="e1",
            ),
        ),
    )
    res = validate_claim_against_truth(
        speaker_role=Role.WEREWOLF,
        speaker_seat=2,
        day=2,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=None,
        claimed_medium=ClaimedMediumResult(target_seat=5, is_wolf=False),
        prior_public_claims=prior,
        executions_so_far=1,
    )
    assert not res.ok
    assert res.reason == REASON_MEDIUM_TARGET_SWAP


def test_fake_medium_verdict_flip_rejected() -> None:
    prior = ClaimerHistory(
        claimer_seat=2,
        medium_claims=(
            ClaimedMediumEntry(
                day=2,
                target_seat=4,
                target_name="席4",
                is_wolf=False,
                declared_at_event_id="e1",
            ),
        ),
    )
    res = validate_claim_against_truth(
        speaker_role=Role.WEREWOLF,
        speaker_seat=2,
        day=2,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=None,
        claimed_medium=ClaimedMediumResult(target_seat=4, is_wolf=True),
        prior_public_claims=prior,
        executions_so_far=1,
    )
    assert not res.ok
    assert res.reason == REASON_MEDIUM_VERDICT_FLIP


# ─── boundary cases ──────────────────────────────────────────────────


def test_villager_with_null_claims_always_passes() -> None:
    """Non-CO speech from a villager (no role-related claims) is fine."""
    res = validate_claim_against_truth(
        speaker_role=Role.VILLAGER,
        speaker_seat=7,
        day=2,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=None,
        claimed_medium=None,
        prior_public_claims=ClaimerHistory(claimer_seat=7),
    )
    assert res.ok


def test_madman_first_seer_claim_passes() -> None:
    res = validate_claim_against_truth(
        speaker_role=Role.MADMAN,
        speaker_seat=8,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        claimed_seer=ClaimedSeerResult(target_seat=3, is_wolf=False),
        claimed_medium=None,
        prior_public_claims=ClaimerHistory(claimer_seat=8),
    )
    assert res.ok
