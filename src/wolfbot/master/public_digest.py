"""Master-side public-info digest builder.

Phase-D: Master is responsible for structuring the public log into
something each NPC bot can fold into its prompt without re-doing the
NLP. This module turns the live `PublicDiscussionState` + recent
`SpeechEvent` rows into a compact, *role-blind* Japanese block. Each
NPC bot receives the same digest regardless of role; their own private
state is what differentiates the eventual decision.

What lands in the digest:

* Active CO claims: seat → role, with counter-CO history.
* Silent seats (alive seats who haven't spoken in the day's discussion).
* Per-seat **addressed-count**: how often each seat has been the
  ``addressed_seat_no`` of another's utterance — a lightweight stand-in
  for a true "pressure" / "stance" score that doesn't require an extra
  LLM analyzer pass.
* Last addressed line: the most recent seat-to-seat callout text so the
  NPC can reply on-topic.
* Recent speeches (last N): full text per utterance so the vote / night
  decision LLM sees what the seer / medium actually claimed. Without
  this the vote prompt only saw 「席9 が seer CO」 and 「席3 が medium
  CO」 — no SQ-黒 detail — and ラキオ ended up voting セツ instead of
  the SQ that ユリコ called black.
* Past votes from completed days: voter → target ledger so the LLM can
  reason about who voted whom historically.

Pure function of the inputs — no I/O, no LLM. Master invokes it once
per outbound `DecideVoteRequest` / `DecideNightActionRequest` /
`SpeakRequest` and stuffs the result into `public_state_summary`.
"""

from __future__ import annotations

from collections.abc import Sequence

from wolfbot.domain.discussion import PublicDiscussionState, SpeechEvent, SpeechSource

# Cap on the recent-speeches block so the prompt stays compact. Mirrors
# `_RECENT_SPEECH_CAP` in `speak_arbiter.py` for parity.
_RECENT_SPEECH_CAP = 20

# Per-utterance text snippet length cap. Any single speech longer than
# this is truncated with an ellipsis so a single rambling line doesn't
# blow the prompt budget.
_SNIPPET_CAP = 200


def build_public_digest(
    *,
    state: PublicDiscussionState,
    recent_events: Sequence[SpeechEvent],
    seat_names: dict[int, str],
    past_votes: Sequence[tuple[int, int, Sequence[tuple[int, int | None]]]] = (),
) -> str:
    """Compose the Japanese digest block.

    ``recent_events`` is a chronological sequence of the speech events
    folded into ``state``. ``phase_baseline`` sentinels are filtered out
    automatically — callers can pass the raw `load_phase` result.

    ``past_votes`` is the completed-day vote ledger as
    ``(day, round, ((voter, target_or_none), ...))`` tuples. Passing an
    empty sequence skips the ballot block entirely.
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
        for seat in ev.addressed_seat_nos or (
            (ev.addressed_seat_no,) if ev.addressed_seat_no is not None else ()
        ):
            addressed_counts[seat] = addressed_counts.get(seat, 0) + 1
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

    # Prefer the multi-addressee set; fall back to the legacy singular
    # field for state objects that haven't been migrated (older fixtures
    # / tests / pre-multi-address rows).
    addressed_set: frozenset[int] = state.last_addressed_seats
    if not addressed_set and state.last_addressed_seat is not None:
        addressed_set = frozenset({state.last_addressed_seat})
    if addressed_set and state.last_addressed_text:
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
        target_label = "、".join(
            f"席{t} {seat_names.get(t, f'席{t}')}"
            for t in sorted(addressed_set)
        )
        snippet = state.last_addressed_text.strip().replace("\n", " ")
        if len(snippet) > 120:
            snippet = snippet[:120] + "…"
        lines.append(
            f"## 直近の名指し\n  {speaker_label} → {target_label}: 「{snippet}」"
        )

    # Recent speeches with content — the bit the vote / night decision
    # LLM was missing. Capped to the trailing N non-baseline events so
    # the prompt stays small even on a 9-NPC chat-heavy phase.
    speech_lines: list[str] = []
    for ev in recent_events:
        if ev.source == SpeechSource.PHASE_BASELINE:
            continue
        if ev.speaker_seat is None or not ev.text:
            continue
        seat_label = (
            f"席{ev.speaker_seat} "
            f"{seat_names.get(ev.speaker_seat, f'席{ev.speaker_seat}')}"
        )
        snippet = ev.text.strip().replace("\n", " ")
        if len(snippet) > _SNIPPET_CAP:
            snippet = snippet[:_SNIPPET_CAP] + "…"
        speech_lines.append(f"  {seat_label}: 「{snippet}」")
    if speech_lines:
        lines.append("## 直近の発言 (古い順)")
        lines.extend(speech_lines[-_RECENT_SPEECH_CAP:])

    if past_votes:
        lines.append("## 公開された投票履歴")
        for day, round_, pairs in past_votes:
            label = "決選投票" if round_ >= 1 else "投票"
            lines.append(f"- day{day} {label}:")
            for voter, target in pairs:
                voter_label = (
                    f"席{voter} {seat_names.get(voter, f'席{voter}')}"
                )
                if target is None:
                    target_label = "棄権"
                else:
                    target_label = (
                        f"席{target} {seat_names.get(target, f'席{target}')}"
                    )
                lines.append(f"    {voter_label} → {target_label}")

    return "\n".join(lines)


__all__ = ["build_public_digest"]
