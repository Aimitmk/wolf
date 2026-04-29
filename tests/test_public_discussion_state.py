"""Bundle 2 tests — PublicDiscussionState fold semantics.

These tests treat `apply_speech_event` and `rebuild_public_state_from_events`
as a pure pair: a `reduce` over the event sequence must produce the same
state as a single `rebuild` call. They also verify the deterministic rules
called out in the spec delta:

  * `alive_seat_nos` comes from the sentinel — dead seats are excluded by
    construction (they were never in the baseline).
  * `silent_seats` = `alive_seat_nos` minus seats with ≥1 non-sentinel event.
  * `co_claims` are recorded in arrival order, deduped per (seat, role).
  * `recent_speech_event_ids` keeps at most 10 ids in arrival order.
"""

from __future__ import annotations

from functools import reduce

from wolfbot.domain.discussion import SpeechSource
from wolfbot.domain.enums import Phase
from wolfbot.services.discussion_service import (
    apply_speech_event,
    make_human_text_event,
    make_npc_generated_event,
    make_phase_baseline,
    make_phase_id,
    rebuild_public_state_from_events,
)


def _seed(alive: list[int], events_payload: list[tuple[int, str]], game_id: str = "g1") -> list:
    phase_id = make_phase_id(game_id, 1, Phase.DAY_DISCUSSION)
    sentinel = make_phase_baseline(
        game_id=game_id,
        phase_id=phase_id,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=alive,
        created_at_ms=0,
    )
    seq = [sentinel]
    for i, (seat, text) in enumerate(events_payload, start=1):
        seq.append(
            make_human_text_event(
                game_id=game_id,
                phase_id=phase_id,
                day=1,
                phase=Phase.DAY_DISCUSSION,
                speaker_seat=seat,
                text=text,
                created_at_ms=i,
            )
        )
    return seq


def test_apply_then_rebuild_agree() -> None:
    events = _seed(
        alive=[1, 2, 3, 4, 5, 6, 7, 8, 9],
        events_payload=[
            (1, "占いCOします"),
            (3, "情報整理しましょう"),
            (3, "P1のCOは普通"),
            (7, "霊媒COです"),
        ],
    )

    folded = reduce(apply_speech_event, events, None)
    rebuilt = rebuild_public_state_from_events(events)

    assert folded is not None and rebuilt is not None
    assert folded.alive_seat_nos == rebuilt.alive_seat_nos
    assert folded.co_claims == rebuilt.co_claims
    assert folded.silent_seats == rebuilt.silent_seats
    assert folded.recent_speech_event_ids == rebuilt.recent_speech_event_ids
    assert folded.speech_counts == rebuilt.speech_counts


def test_silent_seats_excludes_dead_seats() -> None:
    # alive baseline omits seat 5 (dead); so even if "no event for seat 5", it is NOT silent
    events = _seed(
        alive=[1, 2, 3, 4, 6, 7, 8, 9],
        events_payload=[(1, "おはよう"), (2, "はい"), (4, "黙ってる人気になる")],
    )
    state = rebuild_public_state_from_events(events)
    assert state is not None
    assert 5 not in state.alive_seat_nos
    assert 5 not in state.silent_seats
    assert state.silent_seats == frozenset({3, 6, 7, 8, 9})


def test_co_claims_dedup_and_preserve_order() -> None:
    events = _seed(
        alive=list(range(1, 10)),
        events_payload=[
            (4, "占いCO 結果は1白"),
            (4, "占いCO（再）"),
            (7, "霊媒CO"),
            (7, "霊媒CO 続報"),
            (1, "占いCO 対抗"),
        ],
    )
    state = rebuild_public_state_from_events(events)
    assert state is not None
    seq = [(c.seat, c.role_claim) for c in state.co_claims]
    assert seq == [(4, "seer"), (7, "medium"), (1, "seer")]


def test_recent_speech_event_ids_caps_at_10() -> None:
    events = _seed(
        alive=list(range(1, 10)),
        events_payload=[(((i % 9) + 1), f"発言{i}") for i in range(1, 16)],
    )
    state = rebuild_public_state_from_events(events)
    assert state is not None
    assert len(state.recent_speech_event_ids) == 10
    # the cap keeps the LAST 10 by arrival order
    expected_last_10 = [e.event_id for e in events if e.source != SpeechSource.PHASE_BASELINE][-10:]
    assert list(state.recent_speech_event_ids) == expected_last_10


def test_apply_with_only_npc_events_still_silences_alive_baseline() -> None:
    # Even without any human events, an alive seat with zero non-sentinel events stays silent.
    pid = make_phase_id("g1", 1, Phase.DAY_DISCUSSION)
    sentinel = make_phase_baseline(
        game_id="g1",
        phase_id=pid,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=[1, 2, 3],
        created_at_ms=0,
    )
    npc = make_npc_generated_event(
        game_id="g1",
        phase_id=pid,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=1,
        text="hi",
        created_at_ms=1,
    )

    state = rebuild_public_state_from_events([sentinel, npc])
    assert state is not None
    assert state.alive_seat_nos == frozenset({1, 2, 3})
    assert state.silent_seats == frozenset({2, 3})


def test_apply_speech_event_returns_none_without_prior_baseline() -> None:
    # apply_speech_event(None, non_sentinel) must return None — caller hasn't seeded a state yet
    pid = make_phase_id("g1", 1, Phase.DAY_DISCUSSION)
    e = make_human_text_event(
        game_id="g1",
        phase_id=pid,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=1,
        text="hi",
        created_at_ms=1,
    )
    assert apply_speech_event(None, e) is None


def test_rebuild_is_independent_of_caller_supplied_seed() -> None:
    # The rebuild path is documented as needing only `events`; calling it twice
    # on the same input should yield identical states (no shared mutable seed).
    events = _seed(alive=[1, 2, 3], events_payload=[(1, "占いCO"), (2, "霊媒CO")])
    s1 = rebuild_public_state_from_events(events)
    s2 = rebuild_public_state_from_events(events)
    assert s1 is not None and s2 is not None
    assert s1 == s2


def test_co_claim_picks_up_structured_co_declaration_field() -> None:
    """Natural-language utterances ('実は私、占い師なんだ') don't contain
    the legacy '占いCO' marker, but the structured `co_declaration` field
    is authoritative — `_resolve_co_role` prefers it over substring scan
    so NPC speech with no jargon still produces a CoClaim.
    """
    pid = make_phase_id("g1", 1, Phase.DAY_DISCUSSION)
    sentinel = make_phase_baseline(
        game_id="g1",
        phase_id=pid,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=[1, 2, 3],
        created_at_ms=0,
    )
    natural_co = make_npc_generated_event(
        game_id="g1",
        phase_id=pid,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=2,
        text="実は私、占い師なんだ。昨夜は3番を見たけど、白だった。",
        co_declaration="seer",
        created_at_ms=1,
    )
    state = rebuild_public_state_from_events([sentinel, natural_co])
    assert state is not None
    assert [(c.seat, c.role_claim) for c in state.co_claims] == [(2, "seer")]


def test_co_claim_topical_mention_without_structured_field_is_ignored() -> None:
    """A non-CO event that mentions 'CO' textually but has co_declaration=None
    must NOT produce a CoClaim — that's the whole point of the structured
    field replacing fuzzy substring matching for NPC text. Substring
    fallback is reserved for legacy events without the field set."""
    pid = make_phase_id("g1", 1, Phase.DAY_DISCUSSION)
    sentinel = make_phase_baseline(
        game_id="g1",
        phase_id=pid,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=[1, 2, 3],
        created_at_ms=0,
    )
    topical = make_npc_generated_event(
        game_id="g1",
        phase_id=pid,
        day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=2,
        text="他に占い師として名乗る人はいる?",
        co_declaration=None,
        created_at_ms=1,
    )
    state = rebuild_public_state_from_events([sentinel, topical])
    assert state is not None
    assert state.co_claims == ()


def test_co_claim_legacy_substring_fallback_still_works_when_field_absent() -> None:
    """Backwards compat: old events written before the structured field are
    still parsed via `_CO_MARKERS` substring scan when co_declaration=None.
    """
    events = _seed(alive=[1, 2, 3], events_payload=[(1, "占いCOします")])
    state = rebuild_public_state_from_events(events)
    assert state is not None
    assert [(c.seat, c.role_claim) for c in state.co_claims] == [(1, "seer")]


def test_speech_counts_increment_per_speaker() -> None:
    """`speech_counts` records how many non-baseline events each seat has
    produced this phase. The arbiter's pick logic reads this so a
    talkative NPC drops below quieter ones once everyone has spoken at
    least once.
    """
    events = _seed(
        alive=[1, 2, 3],
        events_payload=[
            (1, "ラキオ1"),
            (2, "セツ1"),
            (1, "ラキオ2"),
            (1, "ラキオ3"),
        ],
    )
    state = rebuild_public_state_from_events(events)
    assert state is not None
    assert state.speech_counts == {1: 3, 2: 1}
    # silent_seats stays binary — seat 3 never spoke. The new field is
    # additive and complements (not replaces) the silent baseline.
    assert state.silent_seats == frozenset({3})


def test_speech_counts_excludes_baseline_sentinel() -> None:
    """The `phase_baseline` sentinel has speaker_seat=None, so it must
    not bump any seat's count.
    """
    events = _seed(alive=[1, 2, 3], events_payload=[])
    state = rebuild_public_state_from_events(events)
    assert state is not None
    assert state.speech_counts == {}


def test_multi_addressed_seats_populate_set_and_consume_per_responder() -> None:
    """An NPC addressing multiple seats puts ALL of them in
    ``last_addressed_seats``. When one of them replies, only that
    addressee is removed; the others remain prioritised so the next
    dispatch still favours an unanswered addressee.
    """
    pid = make_phase_id("g1", 1, Phase.DAY_DISCUSSION)
    sentinel = make_phase_baseline(
        game_id="g1", phase_id=pid, day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=[1, 2, 3, 4], created_at_ms=0,
    )
    multi_addr = make_npc_generated_event(
        game_id="g1", phase_id=pid, day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=1, text="セツとジナはどう?",
        addressed_seat_nos=(2, 3),
        created_at_ms=10,
    )
    state_after_addr = rebuild_public_state_from_events([sentinel, multi_addr])
    assert state_after_addr is not None
    assert state_after_addr.last_addressed_seats == frozenset({2, 3})

    # Setsu (seat 2) replies. Gina (seat 3) should still be in the set.
    setsu_reply = make_npc_generated_event(
        game_id="g1", phase_id=pid, day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=2, text="うーん、少し迷うところがあります。",
        created_at_ms=20,
    )
    state_after_reply = rebuild_public_state_from_events(
        [sentinel, multi_addr, setsu_reply]
    )
    assert state_after_reply is not None
    assert state_after_reply.last_addressed_seats == frozenset({3})


def test_multi_address_back_compat_singular_field_promoted() -> None:
    """A SpeechEvent that only sets the legacy ``addressed_seat_no`` (no
    list field) must still appear in ``last_addressed_seats`` so older
    fixtures and pre-multi-address persisted rows keep working.
    """
    pid = make_phase_id("g1", 1, Phase.DAY_DISCUSSION)
    sentinel = make_phase_baseline(
        game_id="g1", phase_id=pid, day=1,
        phase=Phase.DAY_DISCUSSION,
        alive_seat_nos=[1, 2, 3], created_at_ms=0,
    )
    legacy_event = make_npc_generated_event(
        game_id="g1", phase_id=pid, day=1,
        phase=Phase.DAY_DISCUSSION,
        speaker_seat=1, text="セツさん、どう?",
        addressed_seat_no=2,  # singular only
        created_at_ms=10,
    )
    state = rebuild_public_state_from_events([sentinel, legacy_event])
    assert state is not None
    assert state.last_addressed_seats == frozenset({2})
    assert state.last_addressed_seat == 2
