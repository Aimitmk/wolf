"""Tests for the per-seat divination/medium claim aggregator and its
prompt-side rendering through ``build_logic_packet``.

Coverage map:

* :mod:`wolfbot.master.claim.claim_history` — pure fold over SpeechEvent.
* :mod:`wolfbot.master.arbiter.logic_service` — claim block surfacing in
  ``LogicPacket.public_state_summary``.
"""

from __future__ import annotations

from wolfbot.domain.discussion import (
    PublicDiscussionState,
    SpeakerKind,
    SpeechEvent,
    SpeechSource,
)
from wolfbot.domain.enums import Phase
from wolfbot.master.arbiter.logic_service import build_logic_packet
from wolfbot.master.claim.claim_history import (
    ClaimedMediumEntry,
    ClaimedSeerEntry,
    collect_claim_history,
    expected_medium_claim_count_for_day,
    expected_seer_claim_count_for_day,
)


def _speech_event(
    *,
    seat: int,
    day: int,
    text: str = "",
    seer_target: int | None = None,
    seer_is_wolf: bool | None = None,
    medium_target: int | None = None,
    medium_is_wolf: bool | None = None,
    event_id: str | None = None,
    created_at_ms: int = 0,
) -> SpeechEvent:
    return SpeechEvent(
        event_id=event_id or f"ev_{seat}_{day}_{created_at_ms}",
        game_id="g1",
        phase_id=f"g1::day{day}::DAY_DISCUSSION::1",
        day=day,
        phase=Phase.DAY_DISCUSSION,
        source=SpeechSource.NPC_GENERATED,
        speaker_kind=SpeakerKind.NPC,
        speaker_seat=seat,
        text=text,
        claimed_seer_target_seat=seer_target,
        claimed_seer_is_wolf=seer_is_wolf,
        claimed_medium_target_seat=medium_target,
        claimed_medium_is_wolf=medium_is_wolf,
        created_at_ms=created_at_ms,
    )


def test_collect_claim_history_groups_seer_claims_by_seat() -> None:
    events = [
        _speech_event(
            seat=2, day=1, seer_target=1, seer_is_wolf=False, created_at_ms=10,
        ),
        _speech_event(
            seat=9, day=1, seer_target=5, seer_is_wolf=False, created_at_ms=20,
        ),
        _speech_event(
            seat=2, day=2, seer_target=8, seer_is_wolf=False, created_at_ms=30,
        ),
    ]

    history = collect_claim_history(events, seat_names={1: "Comet", 5: "Setsu", 8: "Stella"})

    assert sorted(history.by_seat.keys()) == [2, 9]
    seat2 = history.by_seat[2]
    assert seat2.seer_claims == (
        ClaimedSeerEntry(
            day=1,
            target_seat=1,
            target_name="Comet",
            is_wolf=False,
            declared_at_event_id=events[0].event_id,
        ),
        ClaimedSeerEntry(
            day=2,
            target_seat=8,
            target_name="Stella",
            is_wolf=False,
            declared_at_event_id=events[2].event_id,
        ),
    )
    seat9 = history.by_seat[9]
    assert len(seat9.seer_claims) == 1
    assert seat9.seer_claims[0].target_name == "Setsu"


def test_collect_claim_history_skips_baselines_and_systemless_events() -> None:
    baseline = SpeechEvent(
        event_id="baseline",
        game_id="g1",
        phase_id="g1::day0::DAY_DISCUSSION::1",
        day=0,
        phase=Phase.DAY_DISCUSSION,
        source=SpeechSource.PHASE_BASELINE,
        speaker_kind=SpeakerKind.SYSTEM,
        speaker_seat=None,
        text="",
        alive_seat_nos_json="[1,2]",
        created_at_ms=0,
    )
    speaker_event = _speech_event(
        seat=2,
        day=1,
        seer_target=3,
        seer_is_wolf=True,
        created_at_ms=10,
    )

    history = collect_claim_history([baseline, speaker_event])

    # Only the speaker event surfaced; the baseline is silently filtered.
    assert list(history.by_seat.keys()) == [2]


def test_collect_claim_history_handles_medium_void_result() -> None:
    """Medium claims may carry ``is_wolf=None`` to encode 'no execution
    yesterday → no result today'. The aggregator must preserve the void."""
    event = _speech_event(
        seat=5,
        day=2,
        medium_target=3,
        medium_is_wolf=None,
        created_at_ms=100,
    )

    history = collect_claim_history([event], seat_names={3: "Jonas"})

    seat5 = history.by_seat[5]
    assert seat5.medium_claims == (
        ClaimedMediumEntry(
            day=2,
            target_seat=3,
            target_name="Jonas",
            is_wolf=None,
            declared_at_event_id=event.event_id,
        ),
    )


def test_collect_claim_history_dedupes_same_day_reassertions() -> None:
    """Same (day, target, verdict) restated across multiple speech turns
    in one day's discussion rounds folds to a single ledger row.

    Regression: game ``2026-05-02_09-34-19`` exported two ledger rows
    for the medium claimer's "day 2 シゲミチ白" (round 1 + round 2 of
    the same morning) and four rows for Yuriko's seer claim (with
    "day 3 セツ白" repeated twice). The viewer's CO-history panel
    rendered both as visible duplicates, and the seer-cap header
    chip ("通算 X / 期待 Y") overshot. A real seer never issues two
    cumulative entries for the same morning's NIGHT_K result, so the
    aggregator must collapse identical re-assertions.
    """
    events = [
        _speech_event(
            seat=9,
            day=3,
            seer_target=5,
            seer_is_wolf=False,
            event_id="ev_first",
            created_at_ms=10,
        ),
        # Same speaker, same day, same target+verdict — re-asserted
        # across discussion rounds. Must be folded.
        _speech_event(
            seat=9,
            day=3,
            seer_target=5,
            seer_is_wolf=False,
            event_id="ev_second",
            created_at_ms=20,
        ),
        # Medium claim on the same day, repeated.
        _speech_event(
            seat=8,
            day=2,
            medium_target=6,
            medium_is_wolf=False,
            event_id="ev_med1",
            created_at_ms=5,
        ),
        _speech_event(
            seat=8,
            day=2,
            medium_target=6,
            medium_is_wolf=False,
            event_id="ev_med2",
            created_at_ms=15,
        ),
    ]

    history = collect_claim_history(
        events,
        seat_names={5: "Setsu", 6: "Shigemichi"},
    )

    # Seer dedup: only the first event_id survives, ledger has 1 row.
    assert history.by_seat[9].seer_claims == (
        ClaimedSeerEntry(
            day=3,
            target_seat=5,
            target_name="Setsu",
            is_wolf=False,
            declared_at_event_id="ev_first",
        ),
    )
    # Medium dedup: identical re-assert collapses too.
    assert history.by_seat[8].medium_claims == (
        ClaimedMediumEntry(
            day=2,
            target_seat=6,
            target_name="Shigemichi",
            is_wolf=False,
            declared_at_event_id="ev_med1",
        ),
    )


def test_collect_claim_history_keeps_same_day_target_swap() -> None:
    """A different target or color on the same day is *not* a duplicate
    — it's an inconsistency the validator catches as
    ``seer_target_swap`` / ``seer_verdict_flip``. The ledger must
    preserve both rows so post-game review surfaces the contradiction
    even when the validator was bypassed (e.g. legacy export, or a
    structured claim slipping past the retry loop)."""
    events = [
        _speech_event(
            seat=9,
            day=2,
            seer_target=5,
            seer_is_wolf=False,
            event_id="ev_a",
            created_at_ms=10,
        ),
        # Different target on the same day → kept as a separate row.
        _speech_event(
            seat=9,
            day=2,
            seer_target=4,
            seer_is_wolf=False,
            event_id="ev_b",
            created_at_ms=20,
        ),
        # Same target, flipped verdict → also kept.
        _speech_event(
            seat=9,
            day=2,
            seer_target=5,
            seer_is_wolf=True,
            event_id="ev_c",
            created_at_ms=30,
        ),
    ]

    history = collect_claim_history(events, seat_names={4: "Comet", 5: "Setsu"})

    assert len(history.by_seat[9].seer_claims) == 3


def test_collect_claim_history_drops_partial_seer_claims() -> None:
    """A seer claim missing ``is_wolf`` is meaningless and the
    aggregator drops it rather than guessing a verdict."""
    event = _speech_event(
        seat=2, day=1, seer_target=4, seer_is_wolf=None, created_at_ms=5,
    )

    history = collect_claim_history([event])

    assert history.by_seat == {}


def test_expected_seer_claim_count_for_day_returns_announced_entries() -> None:
    """Real seer's announced-claim count: 1 entry per declared day.

    The claim ledger tags each entry with the day it was *announced*
    (not the night the divination happened on). NIGHT_0 surfaces day-1
    morning → entry day=1; NIGHT_K (K>=1) surfaces day-(K+1) morning →
    entry day=K+1. So by day-N morning the count is exactly N. This
    matches `claim_validator._validate_seer_fake`'s
    "1 entry per declared day" rule (`same_day_priors`) — the prompt
    builder's expected count and the validator's structural rule must
    not disagree, otherwise an "緊急: 結果が不足" nudge tells the LLM
    to declare a 2nd same-day target that the validator instantly
    rejects as `seer_target_swap` (regression observed in game
    7faf339713cf where ジナ got fabrication-capped on day-1 runoff
    and never spoke).
    """
    # Day 0: SETUP / NIGHT_0 only — no morning, no announced results.
    assert expected_seer_claim_count_for_day(0) == 0
    # Day 1 morning: only NIGHT_0's random white announced (1 entry).
    assert expected_seer_claim_count_for_day(1) == 1
    # Day 2 morning: NIGHT_0 + NIGHT_1 announced (2 entries).
    assert expected_seer_claim_count_for_day(2) == 2
    # Day N morning: N entries.
    assert expected_seer_claim_count_for_day(4) == 4
    # Defensive: negative day clamps to zero so the rule reads "no
    # results before the game has begun" rather than blowing up.
    assert expected_seer_claim_count_for_day(-1) == 0


def test_expected_medium_claim_count_for_day_returns_executions_so_far() -> None:
    assert expected_medium_claim_count_for_day(0) == 0
    assert expected_medium_claim_count_for_day(3) == 3
    # Defensive clamp matches the seer helper.
    assert expected_medium_claim_count_for_day(-1) == 0


# ----------------------------------------------------- prompt rendering


def _basic_state() -> PublicDiscussionState:
    return PublicDiscussionState(
        game_id="g1",
        phase_id="g1::day1::DAY_DISCUSSION::1",
        day=1,
    )


def test_logic_packet_summary_includes_claim_history_block() -> None:
    """The arbiter passes the per-seat history to ``build_logic_packet``;
    the rendered block has to surface the claimer name, claim count,
    target name, and verdict glyph (黒/白) for every recorded claim."""
    state = _basic_state()
    history = collect_claim_history(
        [
            _speech_event(
                seat=2, day=1, seer_target=1, seer_is_wolf=False, created_at_ms=10,
            ),
            _speech_event(
                seat=9, day=1, seer_target=5, seer_is_wolf=False, created_at_ms=20,
            ),
        ],
        seat_names={1: "Comet", 5: "Setsu", 2: "Jonas", 9: "Yuriko"},
    )

    packet = build_logic_packet(
        state=state,
        recipient_npc_id="npc_yuriko",
        expires_at_ms=1000,
        now_ms=500,
        seat_names={1: "Comet", 2: "Jonas", 5: "Setsu", 9: "Yuriko"},
        claim_history=history,
    )

    summary = packet.public_state_summary
    assert "公開された占い/霊媒CO結果" in summary
    # Distinct-claimer count vs cap shown in the role header.
    assert "通算 2 件 / 上限 3 件" in summary
    # Per-day expected count (= N entries by day-N morning, one per
    # declared day) shown under the seer header so the LLM has a
    # numeric anchor that matches the validator's same-day rule.
    assert "day1 朝までに通算 1 件まで整合" in summary
    # Per-row format includes seat number + alive/dead tag inline.
    assert "Jonas (席2," in summary
    assert "day1: Comet白" in summary
    assert "Yuriko (席9," in summary
    assert "day1: Setsu白" in summary
    # Top warning banner for the dead-CO awareness rule.
    assert "死亡席の CO も依然として有効" in summary


def test_logic_packet_summary_omits_block_without_history() -> None:
    """Older games and pre-claim-history dispatches must render the
    legacy compact summary without the new heading so the prompt
    surface stays unchanged for back-compat exports."""
    state = _basic_state()
    packet = build_logic_packet(
        state=state,
        recipient_npc_id="npc_x",
        expires_at_ms=1000,
        now_ms=500,
        seat_names={1: "Alice"},
        claim_history=None,
    )
    assert "公開された占い/霊媒CO結果" not in packet.public_state_summary


def test_logic_packet_summary_renders_medium_void_as_no_result() -> None:
    """Medium claims with ``is_wolf=None`` render as '結果なし' so the
    LLM doesn't have to invent a verdict for execution-less days."""
    state = _basic_state()
    history = collect_claim_history(
        [
            _speech_event(
                seat=5, day=2, medium_target=3, medium_is_wolf=None, created_at_ms=100,
            ),
        ],
        seat_names={3: "Jonas", 5: "Shigemichi"},
    )

    packet = build_logic_packet(
        state=state,
        recipient_npc_id="npc_shigemichi",
        expires_at_ms=1000,
        now_ms=500,
        seat_names={3: "Jonas", 5: "Shigemichi"},
        claim_history=history,
    )

    # Medium ledger row format: claimer + seat + alive/dead, then results.
    assert "霊媒CO  通算 1 件 / 上限 2 件" in packet.public_state_summary
    assert "Shigemichi (席5," in packet.public_state_summary
    assert "結果なし" in packet.public_state_summary
