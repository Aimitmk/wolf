"""MasterLogicBuilder — turns `PublicDiscussionState` into per-NPC LogicPackets.

The full design has rich logic candidates (claim chains, support / counter
links, per-seat pressure scores). MVP per the proposal restricts the
deterministic fields to `co_claims` (extracted from text) and `silent_seats`
(alive set minus speakers); `stances` / `pressure` / `open_topics` remain
skeletons and are passed through empty.

This module produces a `LogicPacket` that:

* enumerates the recipient's seat-aware view of CO claims as candidate
  entries (one per CO, each empty support/counter list — the NPC bot uses
  its own persona+role to weight them);
* echoes `silent_seats` as a textual `public_state_summary`;
* sets `expires_at_ms` per the current phase deadline;
* leaves `pressure` empty.

Builder is pure: no I/O, no asyncio. The arbiter passes in the deadline.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from wolfbot.domain.discussion import PublicDiscussionState
from wolfbot.domain.ws_messages import LogicCandidate, LogicPacket, RecentSpeech
from wolfbot.master.claim_history import (
    ClaimHistory,
    expected_seer_claim_count_for_day,
)


def _new_packet_id() -> str:
    return f"lp_{uuid.uuid4().hex[:12]}"


def build_logic_packet(
    *,
    state: PublicDiscussionState,
    recipient_npc_id: str,
    expires_at_ms: int,
    now_ms: int,
    pressure: dict[int, float] | None = None,
    additional_candidates: Iterable[LogicCandidate] = (),
    recent_speeches: Iterable[RecentSpeech] = (),
    past_votes: Iterable[
        tuple[int, int, tuple[tuple[int, int | None], ...]]
    ] = (),
    seat_names: dict[int, str] | None = None,
    claim_history: ClaimHistory | None = None,
) -> LogicPacket:
    """Construct a `LogicPacket` for `recipient_npc_id`.

    The packet is deterministic given the same `state` + `now_ms` save for
    the random `packet_id`. Tests should pin `now_ms` and inspect the rest of
    the payload directly.

    ``seat_names`` is a seat → display_name lookup so the rendered
    summary can refer to players by name instead of ``席N``. Optional
    for back-compat (older callers / tests pass nothing and get the
    legacy seat-only rendering); production callers in
    `SpeakArbiter.dispatch_request` always pass it.
    """
    name_map = seat_names or {}

    def _name(seat: int) -> str:
        return name_map.get(seat) or f"席{seat}"

    candidates: list[LogicCandidate] = []
    for claim in state.co_claims:
        candidates.append(
            LogicCandidate(
                id=f"co-{claim.seat}-{claim.role_claim}",
                claim=f"{_name(claim.seat)} {claim.role_claim}CO",
            )
        )
    candidates.extend(additional_candidates)

    silent_names = (
        "、".join(_name(s) for s in sorted(state.silent_seats))
        if state.silent_seats
        else ""
    )
    silent_repr = (
        f"silent_seats=[{silent_names}]" if silent_names else "silent_seats=[]"
    )
    co_repr = (
        ", ".join(f"{_name(c.seat)}={c.role_claim}" for c in state.co_claims)
        if state.co_claims
        else "(none)"
    )
    summary = f"phase_id={state.phase_id} day={state.day} co_claims=[{co_repr}] {silent_repr}"
    if state.pending_role_callouts:
        # Outstanding "誰か占い師?" / "霊媒師の方どうぞ" requests that no
        # one has answered yet. Real role holders should treat this as a
        # CO trigger; wolf-side NPCs should consider whether to fake CO.
        callouts_repr = ", ".join(sorted(state.pending_role_callouts))
        summary += f" pending_role_callouts=[{callouts_repr}]"
    if state.pending_co_response:
        # First-CO counter-CO window: a role just got its first claim
        # and every uncommitted wolf-side seat (plus the real role-holder
        # when the CO'er was wolf-side) is being rotated through the
        # priority pool. NPCs in the pool see this as "you're being asked
        # now; either counter-CO or skip — the window expires once
        # everyone has been asked".
        co_response_repr = ", ".join(sorted(state.pending_co_response))
        summary += f" pending_co_response=[{co_response_repr}]"
    if claim_history is not None and claim_history.by_seat:
        # Public per-claimer divination/medium history. Every NPC sees
        # the same record — real roles use it to keep their own past
        # results consistent in speech, fake-CO wolves see their own
        # prior lies and either commit to them or get caught when they
        # contradict themselves. Compact rendering keeps the prompt
        # token budget bounded even on day 4+.
        summary += "\n\n## 公開された占い/霊媒CO結果 (公式記録)\n"
        # Expected count rule: a real seer at day N has claimed N + 1
        # results (NIGHT_0 random white + one per night). The line
        # below surfaces it once so the LLM has a numeric anchor.
        expected = expected_seer_claim_count_for_day(state.day)
        summary += (
            f"(占いCO: 通算 {expected} 件まで整合。これより少ない/多い結果は破綻)\n"
        )
        for seat_no in sorted(claim_history.by_seat.keys()):
            history = claim_history.by_seat[seat_no]
            who = _name(seat_no)
            if history.seer_claims:
                seer_summary = ", ".join(
                    f"day{c.day}: {c.target_name}{'黒' if c.is_wolf else '白'}"
                    for c in history.seer_claims
                )
                summary += (
                    f"- {who} (占いCO 通算 {len(history.seer_claims)} 件): "
                    f"{seer_summary}\n"
                )
            if history.medium_claims:
                medium_summary = ", ".join(
                    f"day{c.day}: "
                    + (
                        f"{c.target_name}"
                        + ("黒" if c.is_wolf is True else "白" if c.is_wolf is False else "結果なし")
                    )
                    for c in history.medium_claims
                )
                summary += (
                    f"- {who} (霊媒CO 通算 {len(history.medium_claims)} 件): "
                    f"{medium_summary}\n"
                )
        summary = summary.rstrip()
    # Prefer the multi-addressee set; fall back to the legacy singular
    # field for state objects that haven't been migrated (e.g. test
    # fixtures that only set `last_addressed_seat`).
    addressed_seats: frozenset[int] = state.last_addressed_seats
    if not addressed_seats and state.last_addressed_seat is not None:
        addressed_seats = frozenset({state.last_addressed_seat})
    if addressed_seats:
        speaker_repr = (
            _name(state.last_addressed_speaker_seat)
            if state.last_addressed_speaker_seat is not None
            else "human"
        )
        # Truncate the spoken line so the packet stays small even if the
        # speaker rambled. NPCs only need the gist to respond on-topic.
        utter = state.last_addressed_text.strip().replace("\n", " ")
        if len(utter) > 160:
            utter = utter[:160] + "…"
        sorted_seats = sorted(addressed_seats)
        addr_repr = (
            _name(sorted_seats[0])
            if len(sorted_seats) == 1
            else "[" + "、".join(_name(s) for s in sorted_seats) + "]"
        )
        summary += (
            f" last_address={addr_repr}"
            f" from={speaker_repr} text=\"{utter}\""
        )

    return LogicPacket(
        ts=now_ms,
        trace_id=f"lp-{state.phase_id}-{recipient_npc_id}",
        packet_id=_new_packet_id(),
        phase_id=state.phase_id,
        recipient_npc_id=recipient_npc_id,
        public_state_summary=summary,
        logic_candidates=tuple(candidates),
        pressure=pressure or {},
        expires_at_ms=expires_at_ms,
        recent_speeches=tuple(recent_speeches),
        past_votes=tuple(past_votes),
        pending_role_callouts=tuple(sorted(state.pending_role_callouts)),
    )


__all__ = ["build_logic_packet"]
