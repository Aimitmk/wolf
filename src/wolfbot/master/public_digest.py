"""Master-side public-info digest builder.

Phase-D: Master is responsible for structuring the public log into
something each NPC bot can fold into its prompt without re-doing the
NLP. This module turns the live `PublicDiscussionState` + recent
`SpeechEvent` rows into a compact, *role-blind* Japanese block. Each
NPC bot receives the same digest regardless of role; their own private
state is what differentiates the eventual decision.

What lands in the digest (the "中等" digest the user sealed in the
Phase-D spec):

* Active CO claims: seat → role, with counter-CO history.
* Silent seats (alive seats who haven't spoken in the active phase).
* Per-seat **addressed-count**: how often each seat has been the
  ``addressed_seat_no`` of another's utterance — a lightweight stand-in
  for a true "pressure" / "stance" score that doesn't require an extra
  LLM analyzer pass.
* Last addressed line: the most recent seat-to-seat callout text so the
  NPC can reply on-topic.

Pure function of the inputs — no I/O, no LLM. Master invokes it once
per outbound `DecideVoteRequest` / `DecideNightActionRequest` /
`SpeakRequest` and stuffs the result into `public_state_summary`.
"""

from __future__ import annotations

from collections.abc import Sequence

from wolfbot.domain.discussion import PublicDiscussionState, SpeechEvent, SpeechSource


def build_public_digest(
    *,
    state: PublicDiscussionState,
    recent_events: Sequence[SpeechEvent],
    seat_names: dict[int, str],
) -> str:
    """Compose the Japanese digest block.

    ``recent_events`` is a chronological sequence of the speech events
    folded into ``state``. ``phase_baseline`` sentinels are filtered out
    automatically — callers can pass the raw `load_phase` result.
    """
    lines: list[str] = []

    co_lines: list[str] = []
    if state.co_claims:
        for c in state.co_claims:
            name = seat_names.get(c.seat, f"席{c.seat}")
            co_lines.append(f"  席{c.seat} {name}: {c.role_claim}")
    lines.append("## CO 状況")
    lines.extend(co_lines or ["  (まだ誰も CO していない)"])

    if state.silent_seats:
        silent_str = "、".join(
            f"席{s} {seat_names.get(s, f'席{s}')}"
            for s in sorted(state.silent_seats)
        )
        lines.append(f"## 未発言の生存席\n  {silent_str}")
    else:
        lines.append("## 未発言の生存席\n  (なし)")

    # Per-seat addressed count — counts how many times each seat has
    # been the explicit `addressed_seat_no` of another's utterance.
    # Higher = more pointed-at. Sorted descending so the prompt header
    # surfaces the most-pressured seats first.
    addressed_counts: dict[int, int] = {}
    for ev in recent_events:
        if ev.source == SpeechSource.PHASE_BASELINE:
            continue
        if ev.addressed_seat_no is None:
            continue
        addressed_counts[ev.addressed_seat_no] = (
            addressed_counts.get(ev.addressed_seat_no, 0) + 1
        )
    if addressed_counts:
        ranked = sorted(
            addressed_counts.items(), key=lambda kv: (-kv[1], kv[0])
        )
        rank_lines = [
            f"  席{seat_no} {seat_names.get(seat_no, f'席{seat_no}')}: {count}回"
            for seat_no, count in ranked
        ]
        lines.append("## 名指しされた回数 (多い順)")
        lines.extend(rank_lines)

    if state.last_addressed_seat is not None and state.last_addressed_text:
        speaker = (
            state.last_addressed_speaker_seat
            if state.last_addressed_speaker_seat is not None
            else None
        )
        speaker_label = (
            f"席{speaker} {seat_names.get(speaker, f'席{speaker}')}"
            if speaker is not None
            else "人間"
        )
        target = state.last_addressed_seat
        target_label = f"席{target} {seat_names.get(target, f'席{target}')}"
        snippet = state.last_addressed_text.strip().replace("\n", " ")
        if len(snippet) > 120:
            snippet = snippet[:120] + "…"
        lines.append(
            f"## 直近の名指し\n  {speaker_label} → {target_label}: 「{snippet}」"
        )

    return "\n".join(lines)


__all__ = ["build_public_digest"]
