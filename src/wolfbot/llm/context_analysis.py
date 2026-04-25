"""Pure analysis layer that pre-digests public game logs for the LLM prompt.

`analyze_context()` walks `public_logs` (already filtered by game_id by the
caller) plus the current `players`/`seats` snapshot and returns a
`ContextAnalysis` describing:

- claimed seer / medium / knight COs and their (white/black/guard) results,
- a board label such as "3-1", "2-2", "2-1", "1-2", "1-1",
- a rope summary (alive / dead / ropes_left / risk note),
- a per-seat estimate memo built from public claims only.

The module is intentionally I/O-free: no DB, Discord, openai client, or
asyncio. It runs in memory at prompt-build time and is consumed by
`prompt_builder.build_user_context`.

The parser is conservative: ambiguous mentions resolve to `target_seat=None`
rather than guessing, and `RoleEstimate` carries an explicit `confidence`
field so the LLM never sees these notes as hard facts.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from wolfbot.domain.enums import Role
from wolfbot.domain.models import Player, Seat

VILLAGE_STARTING_ROPES = 4
PLAYER_SPEECH_KIND = "PLAYER_SPEECH"

ResultKind = Literal["SEER", "MEDIUM", "GUARD"]
ResultColor = Literal["WHITE", "BLACK", "GUARD", "GJ"]
Confidence = Literal["low", "medium", "high"]

# CO regexes — match the spec phrases in §1. Conservative: substring search,
# no negation handling. False positives surface as low/medium confidence.
_SEER_CO_RE = re.compile(r"占い師CO|占いCO|占い師として出|占い師です|占いです")
_MEDIUM_CO_RE = re.compile(r"霊媒師CO|霊媒CO|霊媒師です|霊媒です")
_KNIGHT_CO_RE = re.compile(r"騎士CO|狩人CO|騎士です|狩人です")
_SEAT_NUM_RE = re.compile(r"席(\d+)")

# Order matters in `_color_of`: longer markers first so e.g. "人狼でした"
# is not pre-empted by a stray "白" elsewhere in the segment.
_BLACK_HINTS: tuple[str, ...] = ("黒判定", "人狼でした", "人狼だった", "人狼であった", "黒")
_WHITE_HINTS: tuple[str, ...] = (
    "白判定",
    "人狼ではありませんでした",
    "人狼ではありません",
    "人狼ではなかった",
    "人狼ではない",
    "白",
)
_MEDIUM_KIND_HINTS: tuple[str, ...] = ("霊媒結果", "処刑された")
_GUARD_VERB_HINTS: tuple[str, ...] = ("護衛", "守った", "護衛先")
_GJ_HINTS: tuple[str, ...] = ("GJ", "平和")


@dataclass(frozen=True, slots=True)
class ClaimedRole:
    actor_seat: int
    role: Role
    day: int
    raw_text: str


@dataclass(frozen=True, slots=True)
class ClaimedResult:
    actor_seat: int
    target_seat: int | None
    kind: ResultKind
    result: ResultColor | None
    day: int
    raw_text: str


@dataclass(frozen=True, slots=True)
class BoardClassification:
    seer_co_count: int
    medium_co_count: int
    knight_co_count: int
    label: str


@dataclass(frozen=True, slots=True)
class RopeSummary:
    alive_count: int
    dead_count: int
    ropes_left: int
    starting_ropes: int
    risk_note: str


@dataclass(frozen=True, slots=True)
class RoleEstimate:
    seat_no: int
    display_name: str
    alive: bool
    public_claims: tuple[str, ...]
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class ContextAnalysis:
    claimed_roles: tuple[ClaimedRole, ...]
    claimed_results: tuple[ClaimedResult, ...]
    board: BoardClassification
    ropes: RopeSummary
    role_estimates: tuple[RoleEstimate, ...]


def _safe_text(log: dict[str, object]) -> str:
    text = log.get("text")
    return text if isinstance(text, str) else ""


def _safe_actor_seat(log: dict[str, object], seat_nos: set[int]) -> int | None:
    actor = log.get("actor_seat")
    if isinstance(actor, bool) or not isinstance(actor, int):
        return None
    return actor if actor in seat_nos else None


def _safe_day(log: dict[str, object]) -> int:
    day = log.get("day")
    if isinstance(day, bool) or not isinstance(day, int):
        return 0
    return day


def _is_player_speech(log: dict[str, object]) -> bool:
    return log.get("kind") == PLAYER_SPEECH_KIND


def _color_of(segment: str) -> ResultColor | None:
    """Inspect a text window; return WHITE / BLACK / None."""
    for hint in _BLACK_HINTS:
        if hint in segment:
            return "BLACK"
    for hint in _WHITE_HINTS:
        if hint in segment:
            return "WHITE"
    return None


def _classify_result_kind(text: str) -> ResultKind | None:
    """Decide whether a speech looks like a result/guard claim."""
    if any(h in text for h in _MEDIUM_KIND_HINTS):
        return "MEDIUM"
    has_guard_verb = any(h in text for h in _GUARD_VERB_HINTS)
    has_gj = any(h in text for h in _GJ_HINTS)
    if has_gj or (has_guard_verb and _SEAT_NUM_RE.search(text) is not None):
        return "GUARD"
    if any(h in text for h in _BLACK_HINTS) or any(h in text for h in _WHITE_HINTS):
        return "SEER"
    return None


def parse_claims(
    public_logs: Sequence[dict[str, object]], seats: Sequence[Seat]
) -> tuple[ClaimedRole, ...]:
    """Extract seer / medium / knight CO claims from PLAYER_SPEECH public logs."""
    seat_nos = {s.seat_no for s in seats}
    out: list[ClaimedRole] = []
    for log in public_logs:
        if not _is_player_speech(log):
            continue
        actor = _safe_actor_seat(log, seat_nos)
        if actor is None:
            continue
        text = _safe_text(log)
        if not text:
            continue
        day = _safe_day(log)
        if _SEER_CO_RE.search(text) is not None:
            out.append(ClaimedRole(actor_seat=actor, role=Role.SEER, day=day, raw_text=text))
        if _MEDIUM_CO_RE.search(text) is not None:
            out.append(ClaimedRole(actor_seat=actor, role=Role.MEDIUM, day=day, raw_text=text))
        if _KNIGHT_CO_RE.search(text) is not None:
            out.append(ClaimedRole(actor_seat=actor, role=Role.KNIGHT, day=day, raw_text=text))
    return tuple(out)


def _emit_seat_token_results(
    text: str,
    actor: int,
    kind: ResultKind,
    day: int,
    seat_nos: set[int],
) -> tuple[list[ClaimedResult], set[int]]:
    """Sweep `席N` mentions, look for a color in the window up to the next 席N."""
    out: list[ClaimedResult] = []
    emitted: set[int] = set()
    matches = list(_SEAT_NUM_RE.finditer(text))
    for i, m in enumerate(matches):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if n not in seat_nos or n in emitted:
            continue
        window_start = m.end()
        window_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        window = text[window_start : min(window_end, window_start + 60)]
        color = _color_of(window)
        if color is None:
            continue
        emitted.add(n)
        out.append(
            ClaimedResult(
                actor_seat=actor,
                target_seat=n,
                kind=kind,
                result=color,
                day=day,
                raw_text=text,
            )
        )
    return out, emitted


def _emit_display_name_results(
    text: str,
    actor: int,
    kind: ResultKind,
    day: int,
    seats: Sequence[Seat],
    already_emitted: set[int],
) -> list[ClaimedResult]:
    """Bare-name fallback: emit only when display_name is unique across seats."""
    out: list[ClaimedResult] = []
    name_counts: dict[str, int] = {}
    for s in seats:
        if s.display_name:
            name_counts[s.display_name] = name_counts.get(s.display_name, 0) + 1
    for seat in seats:
        if seat.seat_no in already_emitted:
            continue
        name = seat.display_name
        if not name or name not in text:
            continue
        if name_counts.get(name, 0) != 1:
            continue
        idx = text.find(name)
        window = text[idx + len(name) : idx + len(name) + 40]
        color = _color_of(window)
        if color is None:
            continue
        already_emitted.add(seat.seat_no)
        out.append(
            ClaimedResult(
                actor_seat=actor,
                target_seat=seat.seat_no,
                kind=kind,
                result=color,
                day=day,
                raw_text=text,
            )
        )
    return out


def parse_results(
    public_logs: Sequence[dict[str, object]], seats: Sequence[Seat]
) -> tuple[ClaimedResult, ...]:
    """Extract divination / medium / guard result claims from PLAYER_SPEECH logs."""
    seat_nos = {s.seat_no for s in seats}
    out: list[ClaimedResult] = []
    for log in public_logs:
        if not _is_player_speech(log):
            continue
        actor = _safe_actor_seat(log, seat_nos)
        if actor is None:
            continue
        text = _safe_text(log)
        if not text:
            continue
        day = _safe_day(log)
        kind = _classify_result_kind(text)
        if kind is None:
            continue
        if kind == "GUARD":
            target: int | None = None
            seat_match = _SEAT_NUM_RE.search(text)
            if seat_match is not None:
                try:
                    candidate = int(seat_match.group(1))
                except ValueError:
                    candidate = -1
                if candidate in seat_nos:
                    target = candidate
            if target is None:
                # Fall back to unique display_name.
                name_counts: dict[str, int] = {}
                for s in seats:
                    if s.display_name:
                        name_counts[s.display_name] = name_counts.get(s.display_name, 0) + 1
                for s in seats:
                    if (
                        s.display_name
                        and s.display_name in text
                        and name_counts.get(s.display_name, 0) == 1
                    ):
                        target = s.seat_no
                        break
            if any(h in text for h in _GJ_HINTS):
                guard_result: ResultColor | None = "GJ"
            elif target is not None:
                guard_result = "GUARD"
            else:
                guard_result = None
            if target is None and guard_result is None:
                continue
            out.append(
                ClaimedResult(
                    actor_seat=actor,
                    target_seat=target,
                    kind="GUARD",
                    result=guard_result,
                    day=day,
                    raw_text=text,
                )
            )
            continue
        # SEER or MEDIUM
        seat_emits, emitted = _emit_seat_token_results(text, actor, kind, day, seat_nos)
        out.extend(seat_emits)
        out.extend(_emit_display_name_results(text, actor, kind, day, seats, emitted))
        if emitted:
            continue
        # Bare color (no resolved target) — still useful as a low-info signal.
        bare_color = _color_of(text)
        if bare_color is not None:
            out.append(
                ClaimedResult(
                    actor_seat=actor,
                    target_seat=None,
                    kind=kind,
                    result=bare_color,
                    day=day,
                    raw_text=text,
                )
            )
    return tuple(out)


def classify_board(claimed_roles: Sequence[ClaimedRole]) -> BoardClassification:
    """Count unique seats per role-CO; map to a board label."""
    seers: set[int] = set()
    mediums: set[int] = set()
    knights: set[int] = set()
    for c in claimed_roles:
        if c.role is Role.SEER:
            seers.add(c.actor_seat)
        elif c.role is Role.MEDIUM:
            mediums.add(c.actor_seat)
        elif c.role is Role.KNIGHT:
            knights.add(c.actor_seat)
    s, m, k = len(seers), len(mediums), len(knights)
    label_map: dict[tuple[int, int], str] = {
        (3, 1): "3-1",
        (2, 2): "2-2",
        (2, 1): "2-1",
        (1, 2): "1-2",
        (1, 1): "1-1",
    }
    if (s, m) in label_map:
        label = label_map[(s, m)]
    elif s == 0 and m == 0:
        label = "CO なし/未展開"
    else:
        label = "その他/未分類"
    return BoardClassification(
        seer_co_count=s,
        medium_co_count=m,
        knight_co_count=k,
        label=label,
    )


def calculate_rope_summary(players: Sequence[Player]) -> RopeSummary:
    alive = sum(1 for p in players if p.alive)
    dead = len(players) - alive
    ropes_left = max(0, (alive - 1) // 2)
    if alive >= 6:
        risk = f"残り処刑回数の目安: {ropes_left} 縄。終盤までは通常進行。"
    elif alive >= 4:
        risk = f"残り処刑回数の目安: {ropes_left} 縄。PP/RPP の可能性を確認してください。"
    elif alive == 3:
        risk = f"残り処刑回数の目安: {ropes_left} 縄。最終局面: PP/RPP に厳重注意。"
    else:
        risk = f"残り処刑回数の目安: {ropes_left} 縄。決着局面。"
    return RopeSummary(
        alive_count=alive,
        dead_count=dead,
        ropes_left=ropes_left,
        starting_ropes=VILLAGE_STARTING_ROPES,
        risk_note=risk,
    )


def estimate_public_roles(
    seats: Sequence[Seat],
    players: Sequence[Player],
    claimed_roles: Sequence[ClaimedRole],
    claimed_results: Sequence[ClaimedResult],
    board: BoardClassification,
) -> tuple[RoleEstimate, ...]:
    """Build per-seat memos from public claims only. Never reads `Player.role`."""
    players_by_no = {p.seat_no: p for p in players}
    co_by_seat: dict[int, set[Role]] = {}
    for c in claimed_roles:
        co_by_seat.setdefault(c.actor_seat, set()).add(c.role)
    incoming: dict[int, set[ResultColor]] = {}
    for r in claimed_results:
        if r.target_seat is None or r.result not in ("WHITE", "BLACK"):
            continue
        incoming.setdefault(r.target_seat, set()).add(r.result)
    outgoing: set[int] = set()
    for r in claimed_results:
        if r.target_seat is not None:
            outgoing.add(r.actor_seat)

    estimates: list[RoleEstimate] = []
    for seat in sorted(seats, key=lambda s: s.seat_no):
        player = players_by_no.get(seat.seat_no)
        alive = bool(player.alive) if player is not None else True
        cos = co_by_seat.get(seat.seat_no, set())
        colors = incoming.get(seat.seat_no, set())
        claims: list[str] = []
        for role, label in (
            (Role.SEER, "占い師CO"),
            (Role.MEDIUM, "霊媒師CO"),
            (Role.KNIGHT, "騎士CO"),
        ):
            if role in cos:
                claims.append(label)
        if len(cos) >= 2:
            claims.append("矛盾CO")
        if Role.SEER in cos:
            claims.append("単独CO" if board.seer_co_count == 1 else "対抗あり")
        if Role.MEDIUM in cos:
            claims.append("単独CO" if board.medium_co_count == 1 else "対抗あり")
        if Role.KNIGHT in cos and board.knight_co_count >= 2:
            claims.append("対抗あり")
        if cos and not alive:
            claims.append("死亡済みCO")
        if "WHITE" in colors and "BLACK" in colors:
            claims.append("パンダ")
        elif "WHITE" in colors:
            claims.append("白もらい")
        elif "BLACK" in colors:
            claims.append("黒もらい")
        if not cos and not colors:
            claims.append("灰")

        if (cos and seat.seat_no in outgoing) or (cos and not alive):
            confidence: Confidence = "high"
        elif cos or colors:
            confidence = "medium"
        else:
            confidence = "low"

        estimates.append(
            RoleEstimate(
                seat_no=seat.seat_no,
                display_name=seat.display_name,
                alive=alive,
                public_claims=tuple(claims),
                confidence=confidence,
            )
        )
    return tuple(estimates)


def _empty_board() -> BoardClassification:
    return BoardClassification(
        seer_co_count=0,
        medium_co_count=0,
        knight_co_count=0,
        label="CO なし/未展開",
    )


def _empty_ropes() -> RopeSummary:
    return RopeSummary(
        alive_count=0,
        dead_count=0,
        ropes_left=0,
        starting_ropes=VILLAGE_STARTING_ROPES,
        risk_note="",
    )


def analyze_context(
    *,
    seats: Sequence[Seat],
    players: Sequence[Player],
    public_logs: Sequence[dict[str, object]],
) -> ContextAnalysis:
    """Top-level orchestrator. Each helper is wrapped so a parser bug never
    aborts prompt construction; on exception the section degrades to empty."""
    try:
        claimed_roles = parse_claims(public_logs, seats)
    except Exception:
        claimed_roles = ()
    try:
        claimed_results = parse_results(public_logs, seats)
    except Exception:
        claimed_results = ()
    try:
        board = classify_board(claimed_roles)
    except Exception:
        board = _empty_board()
    try:
        ropes = calculate_rope_summary(players)
    except Exception:
        ropes = _empty_ropes()
    try:
        estimates = estimate_public_roles(seats, players, claimed_roles, claimed_results, board)
    except Exception:
        estimates = ()
    return ContextAnalysis(
        claimed_roles=claimed_roles,
        claimed_results=claimed_results,
        board=board,
        ropes=ropes,
        role_estimates=estimates,
    )


def render_context_analysis(analysis: ContextAnalysis, seats: Sequence[Seat]) -> str:
    """Render a ContextAnalysis into the four user_context Markdown blocks."""
    seats_by_no = {s.seat_no: s for s in seats}

    def _label(seat_no: int) -> str:
        seat = seats_by_no.get(seat_no)
        if seat is None:
            return f"席{seat_no}"
        return f"席{seat_no} {seat.display_name}"

    seer_seats: list[int] = []
    medium_seats: list[int] = []
    knight_seats: list[int] = []
    seen: dict[Role, set[int]] = {Role.SEER: set(), Role.MEDIUM: set(), Role.KNIGHT: set()}
    for c in analysis.claimed_roles:
        if c.role not in seen or c.actor_seat in seen[c.role]:
            continue
        seen[c.role].add(c.actor_seat)
        if c.role is Role.SEER:
            seer_seats.append(c.actor_seat)
        elif c.role is Role.MEDIUM:
            medium_seats.append(c.actor_seat)
        elif c.role is Role.KNIGHT:
            knight_seats.append(c.actor_seat)

    def _join(seat_nos: list[int]) -> str:
        return ", ".join(_label(n) for n in seat_nos) if seat_nos else "(なし)"

    co_lines = [
        "## CO・判定の機械整理",
        f"- 占い師CO: {_join(seer_seats)}",
        f"- 霊媒師CO: {_join(medium_seats)}",
        f"- 騎士CO: {_join(knight_seats)}",
    ]
    result_strs: list[str] = []
    for r in analysis.claimed_results:
        if r.target_seat is None:
            continue
        if r.kind in ("SEER", "MEDIUM") and r.result in ("WHITE", "BLACK"):
            color_ja = "白" if r.result == "WHITE" else "黒"
            kind_ja = "占い" if r.kind == "SEER" else "霊媒"
            result_strs.append(
                f"{_label(r.actor_seat)} -> {_label(r.target_seat)} {color_ja}({kind_ja})"
            )
        elif r.kind == "GUARD":
            tag = "護衛GJ主張" if r.result == "GJ" else "護衛主張"
            result_strs.append(f"{_label(r.actor_seat)} -> {_label(r.target_seat)} {tag}")
    co_lines.append(f"- 判定主張: {' / '.join(result_strs) if result_strs else '(なし)'}")

    board_lines = [
        "## 盤面分類",
        (
            f"- 公開CO履歴ベース: {analysis.board.label}"
            f" (占{analysis.board.seer_co_count}/霊{analysis.board.medium_co_count}"
            f"/騎{analysis.board.knight_co_count})"
        ),
        "- 注意: これは真役職数ではなく、公開発言から抽出したCO数です。死亡済みCO者も含みます。",
    ]

    rope_lines = [
        "## 縄数・PP/RPPリスク",
        (
            f"- 生存 {analysis.ropes.alive_count} 人 / 死亡 {analysis.ropes.dead_count} 人。"
            f"{analysis.ropes.risk_note} (9人村開始時は{analysis.ropes.starting_ropes}縄)"
        ),
        "- 注意: 残り人狼数と狂人生存は公開情報から推定する必要があります。",
    ]

    estimate_lines = ["## 役職推定メモ (公開情報ベース)"]
    if not analysis.role_estimates:
        estimate_lines.append("- (なし)")
    else:
        for est in analysis.role_estimates:
            label = _label(est.seat_no)
            if not est.alive:
                label = f"{label} (死亡)"
            claims = " / ".join(est.public_claims) if est.public_claims else "情報なし"
            estimate_lines.append(f"- {label}: {claims} / confidence={est.confidence}")
    estimate_lines.append(
        "- 注意: このメモは公開ログからの機械整理であり、真役職や本当の陣営を保証しません。"
        "自分に見えていない役職・狼位置を事実として断言しないでください。"
    )

    return "\n".join([*co_lines, "", *board_lines, "", *rope_lines, "", *estimate_lines])
