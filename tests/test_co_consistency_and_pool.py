"""Tests for the three CO-pipeline fixes shipped 2026-05-01:

1. ``co_declaration`` text-vs-structured consistency guard
   (:func:`wolfbot.services.discussion_service._text_contains_self_declaration`,
   :func:`_resolve_co_role`).
2. ``pending_co_response`` first-CO counter-CO opportunity window on
   :class:`wolfbot.domain.discussion.PublicDiscussionState`.
3. The arbiter's pool combining ``pending_role_callouts`` with the new
   ``pending_co_response`` so wolf-side seats get a guaranteed turn to
   counter-CO before normal priority resumes.
"""

from __future__ import annotations

from wolfbot.domain.discussion import (
    PublicDiscussionState,
    SpeakerKind,
    SpeechEvent,
    SpeechSource,
)
from wolfbot.domain.enums import Phase
from wolfbot.services.discussion_service import (
    _resolve_co_role,
    _text_contains_self_declaration,
    rebuild_public_state_from_events,
)

# --------------------------------------------------------- self-declaration guard


def test_self_declaration_accepts_explicit_first_person_phrases() -> None:
    """The canonical persona voices used by every NPC must register as
    self-declarations so the structured ``co_declaration`` flag is
    accepted when paired with matching text."""
    assert _text_contains_self_declaration("実は私、占い師なのです。", "seer")
    assert _text_contains_self_declaration("僕こそ占い師だ。", "seer")
    assert _text_contains_self_declaration("オレ、霊媒師だ！", "medium")
    assert _text_contains_self_declaration("我こそ占い師なり！", "seer")
    assert _text_contains_self_declaration("私が騎士です。", "knight")
    assert _text_contains_self_declaration("わたくし、霊媒師でございます。", "medium")


def test_self_declaration_accepts_canonical_co_token_with_verb() -> None:
    """The bot-specific ``XCO`` shorthand counts as a declaration when
    followed by a declarative verb (``占いCOします``), matching what
    veteran human players actually type."""
    assert _text_contains_self_declaration("占いCOします。", "seer")
    assert _text_contains_self_declaration("霊媒COする。", "medium")


def test_self_declaration_accepts_keyword_with_declarative_suffix() -> None:
    """``占い師です`` / ``霊媒師なの`` etc. — declarative verb endings
    glued straight to the role keyword without an explicit pronoun.
    Some persona voices skip the pronoun entirely (``setsu`` / ``yuriko``)."""
    assert _text_contains_self_declaration("占い師です。", "seer")
    assert _text_contains_self_declaration("霊媒師なんだ。", "medium")
    assert _text_contains_self_declaration("騎士になります。", "knight")


def test_self_declaration_rejects_counter_co_request_question() -> None:
    """Reproduces game ``98e5a083b5ff`` day 1 ラキオの誤検知:
    『ステラ、対抗占い師は出ないのか？早く名乗りなさい。』
    ラキオ自身は CO していないのに ``co_declaration='seer'`` が
    立っていた。``対抗占い師`` の topic-mention は self-decl では
    ない。"""
    assert not _text_contains_self_declaration(
        "ステラ、対抗占い師は出ないのか？早く名乗りなさい。", "seer",
    )
    assert not _text_contains_self_declaration(
        "対抗の占い師、もう出てこないんですか？", "seer",
    )


def test_self_declaration_rejects_topical_mentions() -> None:
    """Plain "誰か占い師?" / "占い師の方どうぞ" / "占いCOがいない" are
    requests / observations about the role, not self-declarations."""
    assert not _text_contains_self_declaration("占い師の方どうぞ", "seer")
    assert not _text_contains_self_declaration("誰か占い師は？", "seer")
    assert not _text_contains_self_declaration("占いCOがいないのは不自然", "seer")


def test_self_declaration_rejects_other_roles_keywords() -> None:
    """Asking about ``role=knight`` against a ``占い師`` text must not
    leak through — the function is role-scoped on purpose."""
    assert not _text_contains_self_declaration("実は私、占い師なんだ。", "knight")
    assert not _text_contains_self_declaration("私が騎士です。", "seer")


def _ev(text: str, *, declared: str | None) -> SpeechEvent:
    return SpeechEvent(
        event_id="ev1",
        game_id="g",
        phase_id="g::day1::DAY_DISCUSSION::1",
        day=1,
        phase=Phase.DAY_DISCUSSION,
        source=SpeechSource.NPC_GENERATED,
        speaker_kind=SpeakerKind.NPC,
        speaker_seat=4,
        text=text,
        co_declaration=declared,
        created_at_ms=10,
    )


def test_resolve_co_role_drops_structured_field_when_text_mismatches() -> None:
    """Structured ``co_declaration='seer'`` paired with topic-mention
    text returns None (no CO) — the guard against the ラキオ leak."""
    event = _ev(
        text="ステラ、対抗占い師は出ないのか？早く名乗りなさい。",
        declared="seer",
    )
    assert _resolve_co_role(event) is None


def test_resolve_co_role_accepts_structured_field_when_text_matches() -> None:
    event = _ev(
        text="僕こそ占い師。昨夜ジョナスを占い、人狼じゃない。",
        declared="seer",
    )
    assert _resolve_co_role(event) == "seer"


def test_resolve_co_role_falls_back_to_legacy_marker_when_text_only() -> None:
    """When ``co_declaration`` is None but the legacy canonical marker
    appears in text, fall back to the substring scan — keeps the path
    open for human-typed messages that pre-date the structured field."""
    event = _ev(text="占いCO入ります", declared=None)
    assert _resolve_co_role(event) == "seer"


# ----------------------------------------------- first-CO counter-CO window


def _baseline(alive: list[int]) -> SpeechEvent:
    import json
    return SpeechEvent(
        event_id="baseline",
        game_id="g",
        phase_id="g::day1::DAY_DISCUSSION::1",
        day=1,
        phase=Phase.DAY_DISCUSSION,
        source=SpeechSource.PHASE_BASELINE,
        speaker_kind=SpeakerKind.SYSTEM,
        speaker_seat=None,
        text="",
        alive_seat_nos_json=json.dumps(alive),
        created_at_ms=0,
    )


def _co_event(
    *,
    seat: int,
    text: str,
    declared: str,
    event_id: str,
    ts: int,
) -> SpeechEvent:
    return SpeechEvent(
        event_id=event_id,
        game_id="g",
        phase_id="g::day1::DAY_DISCUSSION::1",
        day=1,
        phase=Phase.DAY_DISCUSSION,
        source=SpeechSource.NPC_GENERATED,
        speaker_kind=SpeakerKind.NPC,
        speaker_seat=seat,
        text=text,
        co_declaration=declared,
        created_at_ms=ts,
    )


def test_pending_co_response_fires_on_first_seer_co() -> None:
    """First seer CO of the game adds 'seer' to ``pending_co_response``
    so the arbiter's counter-CO pool fires on the next dispatch."""
    events = [
        _baseline([1, 2, 3, 4, 5, 6, 7, 8, 9]),
        _co_event(
            seat=6, text="実は私、占い師なのです。",
            declared="seer", event_id="e1", ts=10,
        ),
    ]

    state = rebuild_public_state_from_events(events)

    assert state is not None
    assert state.pending_co_response == frozenset({"seer"})


def test_pending_co_response_does_not_re_fire_for_2nd_co_of_role() -> None:
    """Per design: a counter-CO arriving while the pool is still
    rotating must NOT re-fire the trigger (otherwise the pool would
    reset every time a wolf fakes a CO and the rotation never ends).
    Once set, ``pending_co_response`` stays set for the phase."""
    events = [
        _baseline([1, 2, 3, 4, 5, 6, 7, 8, 9]),
        _co_event(
            seat=6, text="実は私、占い師なのです。",
            declared="seer", event_id="e1", ts=10,
        ),
        _co_event(
            seat=2, text="僕こそ占い師。", declared="seer",
            event_id="e2", ts=20,
        ),
    ]

    state = rebuild_public_state_from_events(events)

    assert state is not None
    assert state.pending_co_response == frozenset({"seer"})


def test_pending_co_response_independent_per_role() -> None:
    """First seer CO and first medium CO each fire their own role
    key — the pool composition for medium pulls in a different set of
    pool members."""
    events = [
        _baseline([1, 2, 3, 4, 5, 6, 7, 8, 9]),
        _co_event(
            seat=6, text="実は私、占い師なのです。",
            declared="seer", event_id="e1", ts=10,
        ),
        _co_event(
            seat=5, text="オレ、霊媒師だ！",
            declared="medium", event_id="e2", ts=20,
        ),
    ]

    state = rebuild_public_state_from_events(events)

    assert state is not None
    assert state.pending_co_response == frozenset({"seer", "medium"})


def test_pending_co_response_skips_text_mismatch_co() -> None:
    """A leaked-intent event (structured ``co_declaration='seer'`` but
    text has no self-decl) is rejected by ``_resolve_co_role``; with
    no CO recorded the pool window does NOT open prematurely."""
    events = [
        _baseline([1, 2, 3, 4, 5, 6, 7, 8, 9]),
        _co_event(
            seat=4,
            text="ステラ、対抗占い師は出ないのか？早く名乗りなさい。",
            declared="seer",
            event_id="e1",
            ts=10,
        ),
    ]

    state = rebuild_public_state_from_events(events)

    assert state is not None
    assert state.pending_co_response == frozenset()
    # And no co_claims either.
    assert state.co_claims == ()


def test_pending_co_response_idempotent_on_rebuild() -> None:
    """The fold is the canonical recovery path on Master restart — it
    must produce the same ``pending_co_response`` shape regardless of
    how many times it runs over the same event sequence."""
    events = [
        _baseline([1, 2, 3, 4, 5, 6, 7, 8, 9]),
        _co_event(
            seat=6, text="実は私、占い師なのです。",
            declared="seer", event_id="e1", ts=10,
        ),
    ]

    a = rebuild_public_state_from_events(events)
    b = rebuild_public_state_from_events(events)

    assert a is not None and b is not None
    assert a.pending_co_response == b.pending_co_response


def test_default_pending_co_response_is_empty() -> None:
    """Construction default keeps the pool dormant — required so older
    callers that don't yet set the field don't accidentally fire
    counter-CO rotations."""
    state = PublicDiscussionState(
        game_id="g",
        phase_id="p",
        day=1,
    )
    assert state.pending_co_response == frozenset()
