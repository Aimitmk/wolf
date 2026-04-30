"""Tests for the per-seat divination/medium claim aggregator and its
prompt-side rendering through ``build_logic_packet``.

Coverage map:

* :mod:`wolfbot.master.claim_history` — pure fold over SpeechEvent.
* :mod:`wolfbot.master.logic_service` — claim block surfacing in
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
from wolfbot.master.claim_history import (
    ClaimedMediumEntry,
    ClaimedSeerEntry,
    collect_claim_history,
    expected_medium_claim_count_for_day,
    expected_seer_claim_count_for_day,
)
from wolfbot.master.logic_service import build_logic_packet


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


def test_collect_claim_history_drops_partial_seer_claims() -> None:
    """A seer claim missing ``is_wolf`` is meaningless and the
    aggregator drops it rather than guessing a verdict."""
    event = _speech_event(
        seat=2, day=1, seer_target=4, seer_is_wolf=None, created_at_ms=5,
    )

    history = collect_claim_history([event])

    assert history.by_seat == {}


def test_expected_seer_claim_count_for_day_follows_n_plus_one_rule() -> None:
    assert expected_seer_claim_count_for_day(0) == 1  # NIGHT_0 random white
    assert expected_seer_claim_count_for_day(1) == 2  # + night 0 result
    assert expected_seer_claim_count_for_day(4) == 5
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
    # Expected count rule surfaces the day-N anchor.
    assert "通算 2 件まで整合" in summary
    assert "Jonas (占いCO 通算 1 件): day1: Comet白" in summary
    assert "Yuriko (占いCO 通算 1 件): day1: Setsu白" in summary


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

    assert "Shigemichi (霊媒CO" in packet.public_state_summary
    assert "結果なし" in packet.public_state_summary
