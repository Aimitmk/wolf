"""Unit tests for the pure analysis helpers in `wolfbot.llm.context_analysis`.

These avoid the DB / async fixtures: they construct `Seat`, `Player`, and fake
public-log dicts inline so the logic can be exercised in isolation. The
analyzer must:

- extract seer / medium / knight CO claims from PLAYER_SPEECH-only logs,
- resolve targets via `席N` first and unique display_name as a fallback,
- classify board labels (3-1 / 2-2 / 2-1 / 1-2 / 1-1 / その他 / CO なし),
- compute rope counts from alive players,
- build per-seat memos using only public claims (never `Player.role`),
- swallow internal exceptions so prompt build never aborts.
"""

from __future__ import annotations

import pytest

from wolfbot.domain.enums import Role
from wolfbot.domain.models import Player, Seat
from wolfbot.llm.context_analysis import (
    BoardClassification,
    ClaimedRole,
    analyze_context,
    calculate_rope_summary,
    classify_board,
    estimate_public_roles,
    parse_claims,
    parse_results,
    render_context_analysis,
)


def _seat(seat_no: int, name: str) -> Seat:
    return Seat(
        seat_no=seat_no,
        display_name=name,
        discord_user_id=f"u{seat_no}",
        is_llm=False,
        persona_key=None,
    )


def _player(seat_no: int, *, role: Role | None = None, alive: bool = True) -> Player:
    return Player(seat_no=seat_no, role=role, alive=alive)


def _speech(text: str, *, actor_seat: int | None, day: int = 1) -> dict[str, object]:
    return {"kind": "PLAYER_SPEECH", "text": text, "actor_seat": actor_seat, "day": day}


# --------------------------------------------------------------- parse_claims
@pytest.mark.parametrize(
    "phrase",
    ["占いCO", "占い師CO", "占い師です", "占いです", "占い師として出ます"],
)
def test_parse_claims_extracts_seer_co_variants(phrase: str) -> None:
    seats = [_seat(1, "A"), _seat(2, "B")]
    claims = parse_claims([_speech(f"{phrase}。よろしく。", actor_seat=2)], seats)
    assert any(c.role is Role.SEER and c.actor_seat == 2 for c in claims), claims


@pytest.mark.parametrize("phrase", ["霊媒CO", "霊媒師CO", "霊媒師です", "霊媒です"])
def test_parse_claims_extracts_medium_co_variants(phrase: str) -> None:
    seats = [_seat(1, "A")]
    claims = parse_claims([_speech(f"今日は{phrase}", actor_seat=1)], seats)
    assert any(c.role is Role.MEDIUM and c.actor_seat == 1 for c in claims)


@pytest.mark.parametrize("phrase", ["騎士CO", "狩人CO", "騎士です", "狩人です"])
def test_parse_claims_extracts_knight_co_variants(phrase: str) -> None:
    seats = [_seat(1, "A")]
    claims = parse_claims([_speech(phrase, actor_seat=1)], seats)
    assert any(c.role is Role.KNIGHT and c.actor_seat == 1 for c in claims)


def test_parse_claims_狩人_maps_to_knight_role() -> None:
    seats = [_seat(1, "A")]
    claims = parse_claims([_speech("狩人CO", actor_seat=1)], seats)
    assert claims and claims[0].role is Role.KNIGHT


def test_parse_claims_skips_speech_without_actor_seat() -> None:
    seats = [_seat(1, "A")]
    claims = parse_claims([_speech("占い師CO", actor_seat=None)], seats)
    assert claims == ()


def test_parse_claims_skips_non_player_speech_kinds() -> None:
    seats = [_seat(1, "A")]
    log = {"kind": "EXECUTE", "text": "占い師CO", "actor_seat": 1, "day": 1}
    assert parse_claims([log], seats) == ()


def test_parse_claims_records_multiple_roles_for_same_seat() -> None:
    seats = [_seat(1, "A")]
    logs = [_speech("占い師CO", actor_seat=1), _speech("やっぱり霊媒師CO", actor_seat=1)]
    claims = parse_claims(logs, seats)
    roles = {c.role for c in claims}
    assert roles == {Role.SEER, Role.MEDIUM}


# -------------------------------------------------------------- parse_results
def test_parse_results_seat_token_resolves_target() -> None:
    seats = [_seat(1, "A"), _seat(3, "Carol")]
    results = parse_results([_speech("占い結果: 席3 Carol 白", actor_seat=1)], seats)
    assert any(r.target_seat == 3 and r.kind == "SEER" and r.result == "WHITE" for r in results)


def test_parse_results_unique_display_name_resolves_target() -> None:
    seats = [_seat(1, "A"), _seat(2, "Bob")]
    results = parse_results([_speech("Bob は黒判定でした", actor_seat=1)], seats)
    assert any(r.target_seat == 2 and r.result == "BLACK" for r in results)


def test_parse_results_ambiguous_display_name_target_is_none() -> None:
    # Two seats share the same display_name → bare-name fallback declines to resolve.
    seats = [_seat(1, "Bob"), _seat(2, "Bob"), _seat(3, "Carol")]
    results = parse_results([_speech("Bob は黒", actor_seat=3)], seats)
    bare = [r for r in results if r.target_seat is None]
    bound = [r for r in results if r.target_seat is not None]
    assert bare and not bound


def test_parse_results_extracts_white_black_keywords() -> None:
    seats = [_seat(1, "A"), _seat(2, "B")]
    logs = [
        _speech("席2 白判定", actor_seat=1),
        _speech("席2 黒判定", actor_seat=1),
        _speech("席2は人狼でした", actor_seat=1),
        _speech("席2は人狼ではありませんでした", actor_seat=1),
    ]
    results = parse_results(logs, seats)
    colors = [r.result for r in results if r.target_seat == 2]
    assert "WHITE" in colors
    assert "BLACK" in colors


def test_parse_results_medium_kind_classification() -> None:
    seats = [_seat(1, "A"), _seat(2, "B")]
    results = parse_results([_speech("霊媒結果: 席2は人狼でした", actor_seat=1)], seats)
    assert any(r.kind == "MEDIUM" and r.target_seat == 2 and r.result == "BLACK" for r in results)


def test_parse_results_extracts_guard_claims() -> None:
    seats = [_seat(1, "A"), _seat(3, "C")]
    logs = [
        _speech("昨夜は席3を護衛", actor_seat=1),
        _speech("GJでした！", actor_seat=1),
    ]
    results = parse_results(logs, seats)
    guard_kinds = [r for r in results if r.kind == "GUARD"]
    # At least one bound (席3) and one GJ.
    assert any(r.target_seat == 3 and r.result == "GUARD" for r in guard_kinds)
    assert any(r.result == "GJ" for r in guard_kinds)


def test_parse_results_emits_bare_color_when_target_unresolved() -> None:
    seats = [_seat(1, "A")]
    # No 席N, no name match — but 黒判定 present.
    results = parse_results([_speech("黒判定でした", actor_seat=1)], seats)
    assert results and results[0].target_seat is None and results[0].result == "BLACK"


def test_parse_results_multiple_targets_in_one_speech() -> None:
    seats = [_seat(1, "A"), _seat(2, "B"), _seat(3, "C")]
    results = parse_results([_speech("席2 白、席3 黒", actor_seat=1)], seats)
    targets = {(r.target_seat, r.result) for r in results}
    assert (2, "WHITE") in targets
    assert (3, "BLACK") in targets


# -------------------------------------------------------------- classify_board
def _co(seat: int, role: Role) -> ClaimedRole:
    return ClaimedRole(actor_seat=seat, role=role, day=1, raw_text="")


@pytest.mark.parametrize(
    "seer_seats, medium_seats, expected",
    [
        ([1, 2, 3], [4], "3-1"),
        ([1, 2], [3, 4], "2-2"),
        ([1, 2], [3], "2-1"),
        ([1], [2, 3], "1-2"),
        ([1], [2], "1-1"),
        ([], [], "CO なし/未展開"),
        ([1, 2, 3, 4], [5], "その他/未分類"),
    ],
)
def test_classify_board_labels(
    seer_seats: list[int], medium_seats: list[int], expected: str
) -> None:
    claims = [_co(s, Role.SEER) for s in seer_seats] + [_co(s, Role.MEDIUM) for s in medium_seats]
    board = classify_board(claims)
    assert board.label == expected


def test_classify_board_dead_co_seats_still_counted() -> None:
    # classify_board cares only about claim presence, not alive/dead.
    claims = [_co(1, Role.SEER), _co(2, Role.SEER), _co(3, Role.SEER), _co(4, Role.MEDIUM)]
    assert classify_board(claims).label == "3-1"


def test_classify_board_same_seat_double_co_does_not_double_count() -> None:
    claims = [_co(1, Role.SEER), _co(1, Role.SEER), _co(2, Role.MEDIUM)]
    board = classify_board(claims)
    assert board.seer_co_count == 1
    assert board.medium_co_count == 1
    assert board.label == "1-1"


def test_classify_board_double_role_co_appears_in_both_counts() -> None:
    claims = [_co(1, Role.SEER), _co(1, Role.MEDIUM)]
    board = classify_board(claims)
    assert board.seer_co_count == 1
    assert board.medium_co_count == 1


# ------------------------------------------------------- calculate_rope_summary
@pytest.mark.parametrize(
    "alive, dead, expected_ropes",
    [(9, 0, 4), (7, 2, 3), (5, 4, 2), (3, 6, 1), (2, 7, 0)],
)
def test_calculate_rope_summary(alive: int, dead: int, expected_ropes: int) -> None:
    players = [_player(i, alive=True) for i in range(1, alive + 1)] + [
        _player(i, alive=False) for i in range(alive + 1, alive + dead + 1)
    ]
    ropes = calculate_rope_summary(players)
    assert ropes.alive_count == alive
    assert ropes.dead_count == dead
    assert ropes.ropes_left == expected_ropes
    assert ropes.starting_ropes == 4


def test_rope_summary_endgame_note_at_3_alive() -> None:
    ropes = calculate_rope_summary([_player(i) for i in range(1, 4)])
    assert "最終局面" in ropes.risk_note


def test_rope_summary_pp_warning_at_5_alive() -> None:
    ropes = calculate_rope_summary([_player(i) for i in range(1, 6)])
    assert "PP/RPP" in ropes.risk_note


def test_rope_summary_normal_note_at_9_alive() -> None:
    ropes = calculate_rope_summary([_player(i) for i in range(1, 10)])
    assert "通常進行" in ropes.risk_note


# -------------------------------------------------------- estimate_public_roles
def _board(
    s: int = 0, m: int = 0, k: int = 0, label: str = "CO なし/未展開"
) -> BoardClassification:
    return BoardClassification(seer_co_count=s, medium_co_count=m, knight_co_count=k, label=label)


def test_estimate_self_co_marks_role_co() -> None:
    seats = [_seat(1, "A"), _seat(2, "B")]
    players = [_player(1), _player(2)]
    claims = [_co(1, Role.SEER)]
    board = _board(s=1, label="1-0")  # label irrelevant here
    estimates = estimate_public_roles(seats, players, claims, [], board)
    seat1 = next(e for e in estimates if e.seat_no == 1)
    assert "占い師CO" in seat1.public_claims


def test_estimate_lone_seer_co_marks_単独CO() -> None:
    seats = [_seat(1, "A"), _seat(2, "B")]
    players = [_player(1), _player(2)]
    claims = [_co(1, Role.SEER)]
    board = classify_board(claims)  # seer_count=1
    estimates = estimate_public_roles(seats, players, claims, [], board)
    seat1 = next(e for e in estimates if e.seat_no == 1)
    assert "単独CO" in seat1.public_claims


def test_estimate_multiple_seer_co_marks_対抗あり() -> None:
    seats = [_seat(1, "A"), _seat(2, "B"), _seat(3, "C")]
    players = [_player(1), _player(2), _player(3)]
    claims = [_co(1, Role.SEER), _co(2, Role.SEER)]
    board = classify_board(claims)  # seer_count=2
    estimates = estimate_public_roles(seats, players, claims, [], board)
    e1 = next(e for e in estimates if e.seat_no == 1)
    e2 = next(e for e in estimates if e.seat_no == 2)
    assert "対抗あり" in e1.public_claims and "対抗あり" in e2.public_claims


def test_estimate_white_target_marks_白もらい() -> None:
    seats = [_seat(1, "A"), _seat(2, "B")]
    players = [_player(1), _player(2)]
    results = parse_results([_speech("席2 白", actor_seat=1)], seats)
    estimates = estimate_public_roles(seats, players, [], results, _board())
    seat2 = next(e for e in estimates if e.seat_no == 2)
    assert "白もらい" in seat2.public_claims


def test_estimate_black_target_marks_黒もらい() -> None:
    seats = [_seat(1, "A"), _seat(2, "B")]
    players = [_player(1), _player(2)]
    results = parse_results([_speech("席2 黒", actor_seat=1)], seats)
    estimates = estimate_public_roles(seats, players, [], results, _board())
    seat2 = next(e for e in estimates if e.seat_no == 2)
    assert "黒もらい" in seat2.public_claims


def test_estimate_white_and_black_marks_パンダ() -> None:
    seats = [_seat(1, "A"), _seat(2, "B"), _seat(3, "C")]
    players = [_player(1), _player(2), _player(3)]
    results = parse_results(
        [_speech("席3 白", actor_seat=1), _speech("席3 黒", actor_seat=2)], seats
    )
    estimates = estimate_public_roles(seats, players, [], results, _board())
    seat3 = next(e for e in estimates if e.seat_no == 3)
    assert "パンダ" in seat3.public_claims


def test_estimate_no_co_no_judgment_marks_灰() -> None:
    seats = [_seat(1, "A"), _seat(2, "B")]
    players = [_player(1), _player(2)]
    estimates = estimate_public_roles(seats, players, [], [], _board())
    for est in estimates:
        assert "灰" in est.public_claims


def test_estimate_dead_co_marks_死亡済みCO() -> None:
    seats = [_seat(1, "A"), _seat(2, "B")]
    players = [_player(1, alive=False), _player(2)]
    claims = [_co(1, Role.SEER)]
    estimates = estimate_public_roles(seats, players, claims, [], classify_board(claims))
    seat1 = next(e for e in estimates if e.seat_no == 1)
    assert "死亡済みCO" in seat1.public_claims
    assert seat1.alive is False


def test_estimate_double_role_co_marks_矛盾CO() -> None:
    seats = [_seat(1, "A"), _seat(2, "B")]
    players = [_player(1), _player(2)]
    claims = [_co(1, Role.SEER), _co(1, Role.MEDIUM)]
    estimates = estimate_public_roles(seats, players, claims, [], classify_board(claims))
    seat1 = next(e for e in estimates if e.seat_no == 1)
    assert "矛盾CO" in seat1.public_claims


def test_estimate_does_not_read_player_role() -> None:
    """Even when Player.role is set to a strong-info value (e.g. WEREWOLF), the
    estimate must reflect only public claims. A WEREWOLF with no public CO and
    no incoming judgment must look like 灰 to anyone reading the analysis."""
    seats = [_seat(1, "A"), _seat(2, "B")]
    players = [_player(1, role=Role.WEREWOLF), _player(2, role=Role.SEER)]
    estimates = estimate_public_roles(seats, players, [], [], _board())
    for est in estimates:
        joined = " ".join(est.public_claims)
        assert "人狼" not in joined
        assert "占い師" not in joined
        assert "灰" in est.public_claims


# --------------------------------------------------------- analyze_context
def test_analyze_context_swallows_internal_exceptions() -> None:
    """A malformed log dict (e.g. text=None) must not raise — the analyzer
    degrades gracefully so prompt building never aborts."""
    seats = [_seat(1, "A")]
    players = [_player(1)]
    bad_logs: list[dict[str, object]] = [
        {"kind": "PLAYER_SPEECH", "text": None, "actor_seat": 1, "day": 1},
        {"kind": "PLAYER_SPEECH"},  # missing fields
    ]
    analysis = analyze_context(seats=seats, players=players, public_logs=bad_logs)
    assert analysis.claimed_roles == ()
    assert analysis.claimed_results == ()


def test_analyze_context_end_to_end() -> None:
    seats = [_seat(1, "A"), _seat(2, "B"), _seat(3, "C")]
    players = [_player(1), _player(2), _player(3)]
    logs: list[dict[str, object]] = [
        _speech("占い師COします。 席3 C 白", actor_seat=1),
        _speech("霊媒師CO", actor_seat=2),
    ]
    analysis = analyze_context(seats=seats, players=players, public_logs=logs)
    assert analysis.board.label == "1-1"
    assert analysis.ropes.alive_count == 3
    assert any(c.role is Role.SEER and c.actor_seat == 1 for c in analysis.claimed_roles)
    assert any(c.role is Role.MEDIUM and c.actor_seat == 2 for c in analysis.claimed_roles)
    assert any(r.target_seat == 3 and r.result == "WHITE" for r in analysis.claimed_results)


# ------------------------------------------------------- render_context_analysis
def test_render_includes_all_four_section_headings() -> None:
    seats = [_seat(1, "A")]
    rendered = render_context_analysis(
        analyze_context(seats=seats, players=[_player(1)], public_logs=[]),
        seats,
    )
    assert "## CO・判定の機械整理" in rendered
    assert "## 盤面分類" in rendered
    assert "## 縄数・PP/RPPリスク" in rendered
    assert "## 役職推定メモ (公開情報ベース)" in rendered


def test_render_empty_state_renders_safely() -> None:
    rendered = render_context_analysis(
        analyze_context(seats=[], players=[], public_logs=[]),
        [],
    )
    assert "(なし)" in rendered  # no COs
    assert "CO なし/未展開" in rendered


def test_render_caution_lines_present() -> None:
    rendered = render_context_analysis(
        analyze_context(seats=[_seat(1, "A")], players=[_player(1)], public_logs=[]),
        [_seat(1, "A")],
    )
    # All three cautions appear.
    assert "真役職数ではなく" in rendered
    assert "残り人狼数と狂人生存は公開情報" in rendered
    assert "真役職や本当の陣営を保証しません" in rendered


def test_render_dead_seat_label_includes_marker() -> None:
    seats = [_seat(1, "A"), _seat(2, "B")]
    players = [_player(1, alive=False), _player(2)]
    analysis = analyze_context(
        seats=seats, players=players, public_logs=[_speech("占い師CO", actor_seat=1)]
    )
    rendered = render_context_analysis(analysis, seats)
    assert "席1 A (死亡)" in rendered
