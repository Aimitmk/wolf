"""Per-seat divination/medium claim history.

Pure aggregation: walk a sequence of :class:`SpeechEvent` rows in
chronological order and group every structured claim by claimer seat.
The result is the canonical "what has each seat publicly claimed as
their seer/medium results so far" view.

Why this exists
---------------
A wolf or madman fake-CO seer must keep claiming results consistent
with what they said yesterday. Without a record, the LLM drifts:
real game ``a51615d32274`` (2026-04-30) had Yuriko (wolf, fake seer)
say "シゲミチ白" on day 1, then on day 2 silently drop シゲミチ and
add フ a fabricated "コメット白" picked up from Jonas's earlier claim.
The fix is to surface every prior claim in every subsequent prompt
so the wolf reads its own past lies and either commits to them or
gets caught.

Determinism / restart safety
----------------------------
The aggregator reads only :class:`SpeechEvent` rows, so on Master
restart the history rebuilds verbatim from the persisted store. No
per-game cache is required.

Day-vs-count integrity
----------------------
A real seer at day-N morning has issued ``N`` public divination
claims: 1 for NIGHT_0 (random white, declared day-1 morning), plus
1 per subsequent night surfaced on the next morning. The claim
ledger tags each entry with the day it was *announced*, so by day
N morning the count is exactly N (one entry per declared day,
day=1..N). :func:`expected_seer_claim_count_for_day` exposes the
rule so the prompt builder and viewer can both reference the same
number — and so the count never disagrees with
``claim_validator``'s ``same_day_priors`` (= "1 entry per declared
day") rule. A liar must mirror that count or get caught.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from wolfbot.domain.discussion import SpeechEvent, SpeechSource


@dataclass(frozen=True)
class ClaimedSeerEntry:
    """One announced seer divination — real or fake."""

    day: int
    target_seat: int
    target_name: str
    is_wolf: bool
    declared_at_event_id: str


@dataclass(frozen=True)
class ClaimedMediumEntry:
    """One announced medium result. ``is_wolf=None`` encodes the
    explicit "no execution yesterday → no result today" case."""

    day: int
    target_seat: int
    target_name: str
    is_wolf: bool | None
    declared_at_event_id: str


@dataclass(frozen=True)
class ClaimerHistory:
    """Time-ordered claim list for a single claimer seat."""

    claimer_seat: int
    seer_claims: tuple[ClaimedSeerEntry, ...] = ()
    medium_claims: tuple[ClaimedMediumEntry, ...] = ()


@dataclass(frozen=True)
class ClaimHistory:
    """Per-claimer fold of every structured seer/medium claim in a game.

    ``by_seat`` is keyed on the claimer's seat number. Iteration order
    of seats is sorted ascending so consumers (prompt builder, viewer)
    can render a deterministic listing.
    """

    by_seat: Mapping[int, ClaimerHistory]


def collect_claim_history(
    events: Sequence[SpeechEvent],
    *,
    seat_names: Mapping[int, str] | None = None,
) -> ClaimHistory:
    """Aggregate structured claims per claimer seat.

    Pure: no I/O, no dependencies beyond ``SpeechEvent``. ``seat_names``
    provides the seat → display_name lookup so the rendered entries
    carry human-readable target names. Missing names fall back to
    ``"席N"`` so the function never errors on incomplete lookups.

    The fold:

    * Skips ``phase_baseline`` sentinels and events without a
      ``speaker_seat`` (system messages).
    * For each event, attaches a seer entry iff
      ``claimed_seer_target_seat`` is set, and a medium entry iff
      ``claimed_medium_target_seat`` is set. Both can co-exist on a
      single event in the unlikely case where a single utterance
      announces both kinds at once.
    * Preserves chronological order via the input sequence's order
      (callers MUST pass events sorted by ``created_at_ms`` ASC).
    """
    name_lookup = dict(seat_names or {})

    def _name(seat: int) -> str:
        return name_lookup.get(seat) or f"席{seat}"

    seer_by_seat: dict[int, list[ClaimedSeerEntry]] = {}
    medium_by_seat: dict[int, list[ClaimedMediumEntry]] = {}

    for event in events:
        if event.source == SpeechSource.PHASE_BASELINE:
            continue
        speaker = event.speaker_seat
        if speaker is None:
            continue

        seer_target = event.claimed_seer_target_seat
        seer_verdict = event.claimed_seer_is_wolf
        if seer_target is not None and seer_verdict is not None:
            seer_by_seat.setdefault(speaker, []).append(
                ClaimedSeerEntry(
                    day=event.day,
                    target_seat=seer_target,
                    target_name=_name(seer_target),
                    is_wolf=seer_verdict,
                    declared_at_event_id=event.event_id,
                )
            )

        medium_target = event.claimed_medium_target_seat
        if medium_target is not None:
            medium_by_seat.setdefault(speaker, []).append(
                ClaimedMediumEntry(
                    day=event.day,
                    target_seat=medium_target,
                    target_name=_name(medium_target),
                    is_wolf=event.claimed_medium_is_wolf,
                    declared_at_event_id=event.event_id,
                )
            )

    seats = sorted(set(seer_by_seat) | set(medium_by_seat))
    by_seat = {
        seat: ClaimerHistory(
            claimer_seat=seat,
            seer_claims=tuple(seer_by_seat.get(seat, ())),
            medium_claims=tuple(medium_by_seat.get(seat, ())),
        )
        for seat in seats
    }
    return ClaimHistory(by_seat=by_seat)


def expected_seer_claim_count_for_day(day: int) -> int:
    """Number of seer claim entries a real seer should have announced by day N.

    The claim ledger tags each entry with the day it was *announced*:

    - NIGHT_0's random white is declared on day 1 morning → entry day=1.
    - NIGHT_K's result (for K>=1) is declared on day K+1 morning →
      entry day=K+1.

    So by day N morning the seer has announced exactly N entries
    (one per declared day, day=1..N). This matches
    :func:`wolfbot.master.claim.claim_validator._validate_seer_fake`'s
    "1 entry per declared day" rule (``same_day_priors``).

    Day 0 has no morning discussion (SETUP / NIGHT_0 are transient),
    so the rule reads "no announced results before day 1".
    """
    if day < 1:
        return 0
    return day


def expected_medium_claim_count_for_day(executions_so_far: int) -> int:
    """Number of medium results expected so far.

    Mediums learn one verdict per execution that has actually
    happened. Days without an execution legitimately have no result;
    callers count distinct execution days to compare against the
    medium claimer's announced result count.
    """
    return max(0, executions_so_far)


__all__ = [
    "ClaimHistory",
    "ClaimedMediumEntry",
    "ClaimedSeerEntry",
    "ClaimerHistory",
    "collect_claim_history",
    "expected_medium_claim_count_for_day",
    "expected_seer_claim_count_for_day",
]
