"""Phase-D Master public-info digest builder."""

from __future__ import annotations

from wolfbot.domain.discussion import (
    CoClaim,
    PublicDiscussionState,
    SpeakerKind,
    SpeechEvent,
    SpeechSource,
    make_phase_id,
)
from wolfbot.domain.enums import Phase
from wolfbot.master.public_digest import build_public_digest


def _state(**overrides: object) -> PublicDiscussionState:
    base: dict[str, object] = {
        "game_id": "g1",
        "phase_id": make_phase_id("g1", 1, Phase.DAY_DISCUSSION),
        "day": 1,
        "alive_seat_nos": frozenset({1, 2, 3, 4}),
        "co_claims": (),
        "silent_seats": frozenset(),
    }
    base.update(overrides)
    return PublicDiscussionState(**base)  # type: ignore[arg-type]


def _ev(
    *,
    speaker_seat: int,
    text: str,
    addressed: int | None = None,
    source: SpeechSource = SpeechSource.NPC_GENERATED,
) -> SpeechEvent:
    return SpeechEvent(
        event_id=f"ev-{speaker_seat}-{text[:5]}",
        game_id="g1",
        phase_id=make_phase_id("g1", 1, Phase.DAY_DISCUSSION),
        day=1,
        phase=Phase.DAY_DISCUSSION,
        source=source,
        speaker_kind=(
            SpeakerKind.HUMAN
            if source == SpeechSource.VOICE_STT or source == SpeechSource.TEXT
            else SpeakerKind.NPC
        ),
        speaker_seat=speaker_seat,
        text=text,
        addressed_seat_no=addressed,
        created_at_ms=1000,
    )


def test_digest_renders_co_section_when_empty() -> None:
    state = _state()
    out = build_public_digest(
        state=state, recent_events=[], seat_names={1: "Alice"},
    )
    assert "## CO 状況" in out
    assert "まだ誰も CO していない" in out


def test_digest_renders_co_claims_with_names() -> None:
    state = _state(
        co_claims=(
            CoClaim(seat=2, role_claim="seer", declared_at_event_id="ev1"),
            CoClaim(seat=4, role_claim="medium", declared_at_event_id="ev2"),
        ),
    )
    out = build_public_digest(
        state=state, recent_events=[],
        seat_names={1: "Alice", 2: "Bob", 4: "Dave"},
    )
    assert "席2 Bob: seer" in out
    assert "席4 Dave: medium" in out


def test_digest_lists_silent_seats() -> None:
    state = _state(silent_seats=frozenset({3, 4}))
    out = build_public_digest(
        state=state, recent_events=[],
        seat_names={3: "Carol", 4: "Dave"},
    )
    assert "## 未発言の生存席" in out
    assert "席3 Carol" in out and "席4 Dave" in out


def test_digest_aggregates_addressed_counts_descending() -> None:
    state = _state()
    events = [
        _ev(speaker_seat=1, text="say 1", addressed=2),
        _ev(speaker_seat=3, text="say 3", addressed=2),
        _ev(speaker_seat=4, text="say 4", addressed=2),
        _ev(speaker_seat=1, text="more", addressed=4),
    ]
    out = build_public_digest(
        state=state, recent_events=events,
        seat_names={2: "Bob", 4: "Dave"},
    )
    assert "## 名指しされた回数 (多い順)" in out
    seat2_idx = out.find("席2 Bob: 3回")
    seat4_idx = out.find("席4 Dave: 1回")
    assert seat2_idx != -1 and seat4_idx != -1
    assert seat2_idx < seat4_idx  # higher count first


def test_digest_renders_last_addressed_block() -> None:
    state = _state(
        last_addressed_seat=2,
        last_addressed_speaker_seat=1,
        last_addressed_text="あなたの白判定が信用できないんです",
    )
    out = build_public_digest(
        state=state, recent_events=[],
        seat_names={1: "Alice", 2: "Bob"},
    )
    assert "## 直近の名指し" in out
    assert "席1 Alice → 席2 Bob" in out
    assert "信用できない" in out


def test_digest_truncates_long_addressed_snippet() -> None:
    long_text = "あ" * 200
    state = _state(
        last_addressed_seat=2,
        last_addressed_speaker_seat=1,
        last_addressed_text=long_text,
    )
    out = build_public_digest(
        state=state, recent_events=[],
        seat_names={1: "Alice", 2: "Bob"},
    )
    # Truncated to 120 chars + ellipsis.
    assert "あ" * 120 + "…" in out
    assert long_text not in out


def test_digest_skips_phase_baseline_in_addressed_counts() -> None:
    state = _state()
    events = [
        _ev(speaker_seat=1, text="", source=SpeechSource.PHASE_BASELINE),
        _ev(speaker_seat=1, text="say", addressed=2),
    ]
    out = build_public_digest(
        state=state, recent_events=events, seat_names={2: "Bob"},
    )
    assert "席2 Bob: 1回" in out
