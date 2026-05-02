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

    def _name(seat: int) -> str:
        """Best-effort display_name lookup; falls back to ``席N`` only
        when the roster doesn't carry a name (recovery edge cases)."""
        return seat_names.get(seat) or f"席{seat}"

    co_lines: list[str] = []
    if state.co_claims:
        for c in state.co_claims:
            co_lines.append(f"  {_name(c.seat)}: {c.role_claim}")
    lines.append("## CO 状況")
    lines.extend(co_lines or ["  (まだ誰も CO していない)"])

    if state.silent_seats:
        silent_str = "、".join(_name(s) for s in sorted(state.silent_seats))
        lines.append(f"## 未発言の生存席\n  {silent_str}")
    else:
        lines.append("## 未発言の生存席\n  (なし)")

    # NOTE: A `## 名指しされた回数 (多い順)` ranking used to live here
    # — counted every time a seat was the addressed_seat_no of another
    # utterance. Removed (2026-05-02) because vote/night decision LLMs
    # were reading it as a "this seat is suspicious" signal, producing
    # bandwagon votes against the most-discussed seat (often the human,
    # who naturally draws more callouts as they argue). The recent
    # speeches block below carries the same information in raw form;
    # the LLM should infer pressure from the actual content, not from
    # an aggregated counter that biases toward bandwagon piles-on.

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
        speaker_label = _name(speaker) if speaker is not None else "人間"
        target_label = "、".join(_name(t) for t in sorted(addressed_set))
        snippet = state.last_addressed_text.strip().replace("\n", " ")
        if len(snippet) > 120:
            snippet = snippet[:120] + "…"
        lines.append(f"## 直近の名指し\n  {speaker_label} → {target_label}: 「{snippet}」")

    # Recent speeches with content — the bit the vote / night decision
    # LLM was missing. Capped to the trailing N non-baseline events so
    # the prompt stays small even on a 9-NPC chat-heavy phase.
    speech_lines: list[str] = []
    for ev in recent_events:
        if ev.source == SpeechSource.PHASE_BASELINE:
            continue
        if ev.speaker_seat is None or not ev.text:
            continue
        snippet = ev.text.strip().replace("\n", " ")
        if len(snippet) > _SNIPPET_CAP:
            snippet = snippet[:_SNIPPET_CAP] + "…"
        speech_lines.append(f"  {_name(ev.speaker_seat)}: 「{snippet}」")
    if speech_lines:
        lines.append("## 直近の発言 (古い順)")
        lines.extend(speech_lines[-_RECENT_SPEECH_CAP:])

    if past_votes:
        lines.append("## 公開された投票履歴")
        for day, round_, pairs in past_votes:
            label = "決選投票" if round_ >= 1 else "投票"
            lines.append(f"- day{day} {label}:")
            for voter, target in pairs:
                voter_label = _name(voter)
                target_label = "棄権" if target is None else _name(target)
                lines.append(f"    {voter_label} → {target_label}")

    return "\n".join(lines)


__all__ = ["build_public_digest"]
