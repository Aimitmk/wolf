"""Phase-D NPC decision LLM helpers — prompt builders + parser.

The helpers are pure functions of state + persona + request so they're
easy to unit-test without standing up an LLM client. Coverage:

* `build_vote_prompt` mentions seat, role, candidates, persona name.
* `build_night_prompt` carries the right action label and candidate set.
* `parse_decision` is robust against bad JSON / illegal targets / type
  abuses (boolean masquerading as int).
"""

from __future__ import annotations

from wolfbot.domain.ws_messages import (
    DecideNightActionRequest,
    DecideVoteRequest,
)
from wolfbot.npc.decision.decision_service import (
    build_night_prompt,
    build_vote_prompt,
    parse_decision,
)
from wolfbot.npc.decision.game_state import NpcGameState
from wolfbot.npc.personas import NPC_PERSONAS_BY_KEY


def _state(role: str = "WEREWOLF") -> NpcGameState:
    return NpcGameState(
        game_id="g1",
        seat_no=3,
        persona_key="setsu",
        role=role,
        day_number=1,
        alive_seats=[(1, "Alice"), (3, "セツ"), (5, "Bob")],
        partner_wolves=[(5, "Bob")] if role == "WEREWOLF" else [],
    )


def test_build_vote_prompt_includes_state_and_candidates() -> None:
    persona = NPC_PERSONAS_BY_KEY["setsu"]
    req = DecideVoteRequest(
        ts=1,
        trace_id="t",
        request_id="rv1",
        npc_id="npc_setsu",
        seat_no=3,
        game_id="g1",
        phase_id="g1::day1::DAY_VOTE::1",
        round_=0,
        candidate_seats=((1, "Alice"), (5, "Bob")),
        public_state_summary="phase=DAY_VOTE day=1 co_claims=[(none)]",
        expires_at_ms=10_000,
    )
    system, user = build_vote_prompt(state=_state(), persona=persona, request=req)
    assert "JSON" in system
    # Own identity now leads with display_name; seat number is
    # appended in parens so the LLM still has the data-layer mapping.
    assert "あなた: セツ (席3)" in user
    assert "あなたの役職: WEREWOLF" in user
    # Roster header is the only block where 席N appears alongside the
    # name; the candidate list keeps 席N because the LLM emits a
    # numeric `target_seat`.
    assert "## 参加者 (席番号 → 名前)" in user
    assert "席1 Alice" in user and "席5 Bob" in user
    assert "通常投票" in user
    # Persona name surfaces verbatim so the LLM stays in character.
    assert persona.display_name in user
    # Wolf partner block (private) appears for werewolf seats.
    assert "仲間の人狼" in user


def test_build_vote_prompt_runoff_label() -> None:
    persona = NPC_PERSONAS_BY_KEY["jonas"]
    req = DecideVoteRequest(
        ts=1,
        trace_id="t",
        request_id="rv2",
        npc_id="npc_jonas",
        seat_no=4,
        game_id="g1",
        phase_id="g1::day1::DAY_RUNOFF::1",
        round_=1,
        candidate_seats=((1, "Alice"),),
        expires_at_ms=10_000,
    )
    _system, user = build_vote_prompt(state=_state("SEER"), persona=persona, request=req)
    assert "決選投票" in user


def test_build_night_prompt_per_action_label() -> None:
    persona = NPC_PERSONAS_BY_KEY["setsu"]
    base_kwargs: dict = dict(  # type: ignore[type-arg]
        ts=1,
        trace_id="t",
        request_id="rn",
        npc_id="npc_setsu",
        seat_no=3,
        game_id="g1",
        phase_id="g1::day1::NIGHT::1",
        candidate_seats=((1, "Alice"),),
        expires_at_ms=10_000,
    )
    for kind, label in (
        ("wolf_attack", "人狼の襲撃"),
        ("seer_divine", "占い師の占い"),
        ("knight_guard", "騎士の護衛"),
    ):
        req = DecideNightActionRequest(**{**base_kwargs, "action_kind": kind})
        _system, user = build_night_prompt(state=_state(), persona=persona, request=req)
        assert label in user, f"missing label for {kind}"


def test_parse_decision_returns_target_when_legal() -> None:
    raw = '{"target_seat": 5, "reason": "怪しい"}'
    result = parse_decision(raw, legal_seats=frozenset({1, 5}))
    assert result.target_seat == 5
    assert result.reason_summary == "怪しい"


def test_parse_decision_falls_back_on_illegal_target() -> None:
    raw = '{"target_seat": 9, "reason": "?"}'
    result = parse_decision(raw, legal_seats=frozenset({1, 5}))
    assert result.target_seat is None
    assert result.reason_summary == "illegal_target"


def test_parse_decision_handles_null_target_as_abstain() -> None:
    raw = '{"target_seat": null, "reason": "迷う"}'
    result = parse_decision(raw, legal_seats=frozenset({1, 5}))
    assert result.target_seat is None
    assert result.reason_summary == "迷う"


def test_parse_decision_rejects_boolean_masquerading_as_int() -> None:
    # In Python, ``isinstance(True, int)`` is True. The parser must
    # treat True / False as non-int so the model can't accidentally
    # vote "True".
    raw = '{"target_seat": true, "reason": "x"}'
    result = parse_decision(raw, legal_seats=frozenset({1, 5}))
    assert result.target_seat is None
    assert result.reason_summary == "non_int_target"


def test_parse_decision_drops_malformed_json() -> None:
    result = parse_decision("not json", legal_seats=frozenset({1, 5}))
    assert result.target_seat is None
    assert result.reason_summary == "parse_failed"


def test_parse_decision_drops_top_level_array() -> None:
    result = parse_decision("[1, 2]", legal_seats=frozenset({1, 5}))
    assert result.target_seat is None
    assert result.reason_summary == "not_object"


def test_build_wolf_chat_prompt_includes_role_strategy_block() -> None:
    """Wolf chat must include the WEREWOLF role-strategy block so the
    chat-side decision sees the master tactical rules (multi-CO attack
    avoidance, GJ rebite, info-role priority, knight-candidate scoring).

    Game `38627df1ade1` had wolves agree in chat to attack a seer in a
    3-CO board because the chat prompt was missing the strategy block.
    The night-action prompt does carry it, but by then `wolf_chat_history`
    already locks the wolves to the chat-time consensus.
    """
    from wolfbot.npc.decision.decision_service import build_wolf_chat_prompt

    persona = NPC_PERSONAS_BY_KEY["gina"]
    _system, user = build_wolf_chat_prompt(
        state=_state(role="WEREWOLF"),
        persona=persona,
        candidates=((1, "Alice"), (5, "Bob")),
        public_state_summary="phase=NIGHT day=1",
    )
    # The role-strategy header marks the block's presence.
    assert "## 役職別の戦術ヒント" in user
    # Spot-check that the multi-CO attack-avoidance line from
    # `_ROLE_STRATEGIES[WEREWOLF]` made it through.
    assert "対抗 CO ありの役職" in user
    # And the no-4th-seer-CO rule too (same block).
    assert "占 3 / 霊 2 / 騎 2 の上限を超える追加 CO は出さない" in user
