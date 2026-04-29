"""DeductionService unit tests.

Pure-function pipeline: given CO claims + players + seats + votes, the
service must produce HARD/MEDIUM facts in deterministic order, and each
fact's text must include the seat-token form so LLM seats can resolve
references back to seat numbers.
"""

from __future__ import annotations

from wolfbot.domain.discussion import CoClaim
from wolfbot.domain.models import Player, Seat, Vote
from wolfbot.services.deduction_service import (
    DeducedFact,
    FactConfidence,
    deduce,
    render_facts_block,
)


def _seat(no: int, name: str) -> Seat:
    return Seat(
        seat_no=no,
        display_name=name,
        discord_user_id=None,
        is_llm=True,
        persona_key="setsu",
    )


def _player(no: int, alive: bool = True) -> Player:
    return Player(
        game_id="g1",
        seat_no=no,
        role=None,
        alive=alive,
        died_day=None,
    )


def _claim(seat: int, role: str, event_id: str = "ev") -> CoClaim:
    return CoClaim(seat=seat, role_claim=role, declared_at_event_id=event_id)


def _seats_9() -> list[Seat]:
    return [_seat(i, f"P{i}") for i in range(1, 10)]


def _all_alive() -> list[Player]:
    return [_player(i) for i in range(1, 10)]


def test_no_co_claims_returns_no_facts() -> None:
    facts = deduce(co_claims=(), players=_all_alive(), seats=_seats_9())
    assert facts == ()


def test_single_uncountered_seer_co_yields_hard_map_and_medium_likely_real() -> None:
    """One alive seer claim, no counters → HARD CO map + MEDIUM 'likely real' fact."""
    facts = deduce(
        co_claims=(_claim(3, "seer"),),
        players=_all_alive(),
        seats=_seats_9(),
    )
    assert len(facts) == 2
    # CO map fact (HARD) lists the alive claimant by seat token.
    co_map = facts[0]
    assert co_map.confidence is FactConfidence.HARD
    assert "占い師" in co_map.text
    assert "席3 P3" in co_map.text
    # Likelihood fact (MEDIUM).
    likely = facts[1]
    assert likely.confidence is FactConfidence.MEDIUM
    assert "対抗履歴なし" in likely.text
    assert "席3" in likely.text


def test_counter_co_two_alive_yields_hard_warning() -> None:
    """Two alive seers → HARD counter-CO warning (max 1 real per ruleset).

    No MEDIUM 'likely real' fact when contested.
    """
    facts = deduce(
        co_claims=(_claim(3, "seer"), _claim(7, "seer")),
        players=_all_alive(),
        seats=_seats_9(),
    )
    confidences = [f.confidence for f in facts]
    assert FactConfidence.HARD in confidences
    counter_facts = [f for f in facts if "騙り確定" in f.text]
    assert len(counter_facts) == 1
    assert "占い師" in counter_facts[0].text
    assert counter_facts[0].affected_seats == frozenset({3, 7})
    # No MEDIUM 'likely real' line — both could still be騙り.
    assert all(f.confidence is FactConfidence.HARD for f in facts)


def test_sole_survivor_of_contested_chain_is_not_auto_real() -> None:
    """≥2 historical claimants, 1 alive → MEDIUM warning, not blanket trust."""
    players = [_player(i, alive=(i != 7)) for i in range(1, 10)]
    facts = deduce(
        co_claims=(_claim(3, "seer"), _claim(7, "seer")),
        players=players,
        seats=_seats_9(),
    )
    medium = [f for f in facts if f.confidence is FactConfidence.MEDIUM]
    assert len(medium) == 1
    assert "対抗 CO 履歴あり" in medium[0].text
    assert "自動で真置きしない" in medium[0].text
    assert medium[0].affected_seats == frozenset({3})


def test_co_map_separates_alive_and_dead() -> None:
    players = [_player(i, alive=(i != 5)) for i in range(1, 10)]
    facts = deduce(
        co_claims=(_claim(5, "medium"),),
        players=players,
        seats=_seats_9(),
    )
    co_map = next(f for f in facts if "霊媒師 の名乗り履歴" in f.text)
    assert "生存=(なし)" in co_map.text
    assert "死亡済み=席5 P5" in co_map.text


def test_vote_history_emits_hard_execution_record() -> None:
    """Past day with majority vote → HARD execution timeline fact."""
    votes_d1 = [
        Vote(game_id="g1", day=1, round=0, voter_seat=1, target_seat=2, submitted_at=0),
        Vote(game_id="g1", day=1, round=0, voter_seat=4, target_seat=2, submitted_at=0),
        Vote(game_id="g1", day=1, round=0, voter_seat=7, target_seat=2, submitted_at=0),
        Vote(game_id="g1", day=1, round=0, voter_seat=3, target_seat=5, submitted_at=0),
    ]
    facts = deduce(
        co_claims=(),
        players=_all_alive(),
        seats=_seats_9(),
        votes_by_day={1: votes_d1},
    )
    exec_facts = [f for f in facts if "day 1 処刑" in f.text]
    assert len(exec_facts) == 1
    assert exec_facts[0].confidence is FactConfidence.HARD
    assert "席2 P2" in exec_facts[0].text
    # Voters listed in seat order.
    assert "席1 P1、席4 P4、席7 P7" in exec_facts[0].text


def test_render_facts_block_groups_by_confidence_with_headers() -> None:
    facts = (
        DeducedFact(text="hard A", confidence=FactConfidence.HARD),
        DeducedFact(text="medium A", confidence=FactConfidence.MEDIUM),
        DeducedFact(text="hard B", confidence=FactConfidence.HARD),
    )
    block = render_facts_block(facts)
    # HARD section comes first, with header.
    assert block.index("HARD") < block.index("MEDIUM")
    # All three facts are surfaced.
    assert "- hard A" in block
    assert "- hard B" in block
    assert "- medium A" in block


def test_render_facts_block_handles_empty_input() -> None:
    assert render_facts_block(()) == "(該当なし)"


def test_facts_order_is_deterministic_co_first_then_likelihood_then_votes() -> None:
    """Stable order: CO map → CO likelihood → vote history. Diffable across runs."""
    votes_d1 = [
        Vote(game_id="g1", day=1, round=0, voter_seat=1, target_seat=2, submitted_at=0),
    ]
    facts = deduce(
        co_claims=(_claim(3, "seer"),),
        players=_all_alive(),
        seats=_seats_9(),
        votes_by_day={1: votes_d1},
    )
    texts = [f.text for f in facts]
    co_map_idx = next(i for i, t in enumerate(texts) if "占い師 の名乗り履歴" in t)
    likely_idx = next(i for i, t in enumerate(texts) if "対抗履歴なし" in t)
    vote_idx = next(i for i, t in enumerate(texts) if "day 1 処刑" in t)
    assert co_map_idx < likely_idx < vote_idx
