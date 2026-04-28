"""Public-information deduction layer for LLM seats.

Master derives logically-forced or near-forced facts from public state and
hands them to LLM seats as labeled `DeducedFact` records. The LLM seat's
`JudgmentProfile` axes (`trust_hard_facts`, `trust_medium_facts`,
`contrarian_bias`) shape how each fact is adopted in the persona's voice.

Two confidence bands:

- ``HARD``  — logically forced from public information (counter-CO count,
              vote/execution history). The LLM should treat these as
              ground truth. Persona shapes only the *rhetoric*, not the
              acceptance.
- ``MEDIUM`` — heuristic but useful (a single uncountered CO is *probably*
              real; a sole-survivor CO from a contested chain is *not*
              auto-real). Persona's `trust_medium_facts` decides adoption
              level.

This module is intentionally pure: callers fetch data from the repo and
pass it in. No I/O, no asyncio. The caller (typically `LLMAdapter._ask`)
is responsible for assembly + injection into `build_user_context`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from wolfbot.domain.discussion import CoClaim
from wolfbot.domain.enums import CO_CLAIM_VALUES
from wolfbot.domain.models import Player, Seat, Vote

_INFO_ROLES: tuple[str, ...] = CO_CLAIM_VALUES
_ROLE_JA: Mapping[str, str] = {"seer": "占い師", "medium": "霊媒師", "knight": "騎士"}


class FactConfidence(StrEnum):
    HARD = "HARD"
    MEDIUM = "MEDIUM"


@dataclass(frozen=True)
class DeducedFact:
    text: str
    confidence: FactConfidence
    affected_seats: frozenset[int] = frozenset()


def _seat_token(seat_no: int, seats_by_no: Mapping[int, Seat]) -> str:
    seat = seats_by_no.get(seat_no)
    if seat is None:
        return f"席{seat_no}"
    return f"席{seat_no} {seat.display_name}"


def _co_map_facts(
    co_claims: Sequence[CoClaim],
    players: Sequence[Player],
    seats_by_no: Mapping[int, Seat],
) -> list[DeducedFact]:
    """Per-role CO summary + counter-CO HARD detection.

    Aggregates `CoClaim` rows by role, separating alive vs dead claimants.
    Emits a HARD fact for each role that has any claim, plus a HARD
    'counter-CO' warning when ≥2 currently-alive claimants exist
    (the 9-player ruleset has at most 1 real seer / medium / knight).
    """
    alive_by_seat = {p.seat_no: p.alive for p in players}
    by_role: dict[str, list[int]] = {role: [] for role in _INFO_ROLES}
    for claim in co_claims:
        if claim.role_claim in by_role and claim.seat not in by_role[claim.role_claim]:
            by_role[claim.role_claim].append(claim.seat)

    out: list[DeducedFact] = []
    for role in _INFO_ROLES:
        seats_for_role = by_role[role]
        if not seats_for_role:
            continue
        alive_seats = [s for s in seats_for_role if alive_by_seat.get(s, False)]
        dead_seats = [s for s in seats_for_role if not alive_by_seat.get(s, False)]
        alive_repr = (
            "、".join(_seat_token(s, seats_by_no) for s in alive_seats)
            if alive_seats
            else "(なし)"
        )
        dead_repr = (
            "、".join(_seat_token(s, seats_by_no) for s in dead_seats)
            if dead_seats
            else "(なし)"
        )
        out.append(
            DeducedFact(
                text=(
                    f"{_ROLE_JA[role]} の名乗り履歴: 生存={alive_repr} / 死亡済み={dead_repr}"
                ),
                confidence=FactConfidence.HARD,
                affected_seats=frozenset(seats_for_role),
            )
        )
        if len(alive_seats) >= 2:
            out.append(
                DeducedFact(
                    text=(
                        f"{_ROLE_JA[role]} の生存名乗りが {len(alive_seats)} 人 "
                        f"({'、'.join(_seat_token(s, seats_by_no) for s in alive_seats)})"
                        " — 9 人村の役職分布上、最大 1 人しか真ではない。"
                        "残りは騙り確定。"
                    ),
                    confidence=FactConfidence.HARD,
                    affected_seats=frozenset(alive_seats),
                )
            )
    return out


def _co_likelihood_facts(
    co_claims: Sequence[CoClaim],
    players: Sequence[Player],
    seats_by_no: Mapping[int, Seat],
) -> list[DeducedFact]:
    """MEDIUM-confidence heuristic facts about CO likelihood.

    Two patterns:

    - Single uncountered CO: exactly one historical claimant of role X,
      and no counter-CO ever appeared → likely real (but not certain;
      the lone claimant could still be a sole-騙り).
    - Sole-survivor of contested chain: ≥2 historical claimants but only
      1 alive now → explicitly NOT auto-real (per the project's CO rules
      in `llm_system_prompt.md`).
    """
    alive_by_seat = {p.seat_no: p.alive for p in players}
    by_role: dict[str, list[int]] = {role: [] for role in _INFO_ROLES}
    for claim in co_claims:
        if claim.role_claim in by_role and claim.seat not in by_role[claim.role_claim]:
            by_role[claim.role_claim].append(claim.seat)

    out: list[DeducedFact] = []
    for role in _INFO_ROLES:
        seats_for_role = by_role[role]
        if not seats_for_role:
            continue
        total = len(seats_for_role)
        alive_seats = [s for s in seats_for_role if alive_by_seat.get(s, False)]
        if total == 1 and len(alive_seats) == 1:
            sole = alive_seats[0]
            out.append(
                DeducedFact(
                    text=(
                        f"{_ROLE_JA[role]} の名乗りは {_seat_token(sole, seats_by_no)} "
                        "のみで対抗履歴なし — 真寄りに扱うのが原則 "
                        "(ただし発言・票・判定の整合性に強い破綻があれば疑ってよい)。"
                    ),
                    confidence=FactConfidence.MEDIUM,
                    affected_seats=frozenset({sole}),
                )
            )
        elif total >= 2 and len(alive_seats) == 1:
            sole = alive_seats[0]
            out.append(
                DeducedFact(
                    text=(
                        f"{_ROLE_JA[role]} は対抗 CO 履歴あり ({total} 人) で生存は "
                        f"{_seat_token(sole, seats_by_no)} のみ — 自動で真置きしない。"
                        "狼が情報役を残した・対抗を吊らせて信用を取った可能性も平行して疑う。"
                    ),
                    confidence=FactConfidence.MEDIUM,
                    affected_seats=frozenset({sole}),
                )
            )
    return out


def _vote_history_facts(
    votes_by_day: Mapping[int, Sequence[Vote]],
    seats_by_no: Mapping[int, Seat],
) -> list[DeducedFact]:
    """Per-day execution + voter-target HARD timeline.

    Vote rows are public after each day's execution announcement, so this
    is just a structured restatement: which seat was executed on day N,
    and which voter cast which ballot. LLM seats can derive the same
    from raw logs, but pre-digested per-day rows save tokens and avoid
    parser drift.
    """
    out: list[DeducedFact] = []
    for day in sorted(votes_by_day.keys()):
        votes = votes_by_day[day]
        if not votes:
            continue
        target_counts: dict[int, list[int]] = {}
        for v in votes:
            if v.target_seat is None:
                continue
            target_counts.setdefault(v.target_seat, []).append(v.voter_seat)
        if not target_counts:
            continue
        executed_seat = max(target_counts.items(), key=lambda kv: len(kv[1]))[0]
        executed_voters = sorted(target_counts[executed_seat])
        voter_repr = "、".join(_seat_token(v, seats_by_no) for v in executed_voters)
        affected = {executed_seat, *executed_voters}
        out.append(
            DeducedFact(
                text=(
                    f"day {day} 処刑: {_seat_token(executed_seat, seats_by_no)} "
                    f"(投票: {voter_repr})"
                ),
                confidence=FactConfidence.HARD,
                affected_seats=frozenset(affected),
            )
        )
    return out


def deduce(
    *,
    co_claims: Sequence[CoClaim],
    players: Sequence[Player],
    seats: Sequence[Seat],
    votes_by_day: Mapping[int, Sequence[Vote]] | None = None,
) -> tuple[DeducedFact, ...]:
    """Run the full deduction pipeline and return facts in stable order.

    Order: CO-map facts → CO-likelihood facts → vote-history facts.
    Stable order within each section makes the prompt diffable across runs.
    """
    seats_by_no = {s.seat_no: s for s in seats}
    facts: list[DeducedFact] = []
    facts.extend(_co_map_facts(co_claims, players, seats_by_no))
    facts.extend(_co_likelihood_facts(co_claims, players, seats_by_no))
    if votes_by_day:
        facts.extend(_vote_history_facts(votes_by_day, seats_by_no))
    return tuple(facts)


def render_facts_block(facts: Sequence[DeducedFact]) -> str:
    """Render a list of facts as a human-readable block grouped by confidence.

    Empty facts list → returns ``"(該当なし)"`` so the caller can always
    splice the result into the user-context template without conditional
    branches.
    """
    if not facts:
        return "(該当なし)"
    hard = [f for f in facts if f.confidence is FactConfidence.HARD]
    medium = [f for f in facts if f.confidence is FactConfidence.MEDIUM]
    parts: list[str] = []
    if hard:
        parts.append("### HARD (公開情報から論理的に確定)")
        parts.extend(f"- {f.text}" for f in hard)
    if medium:
        if parts:
            parts.append("")
        parts.append("### MEDIUM (推測根拠あり、確定ではない)")
        parts.extend(f"- {f.text}" for f in medium)
    return "\n".join(parts)


__all__ = [
    "DeducedFact",
    "FactConfidence",
    "deduce",
    "render_facts_block",
]
