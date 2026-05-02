"""MasterLogicBuilder — turns `PublicDiscussionState` into per-NPC LogicPackets.

The full design has rich logic candidates (claim chains, support / counter
links, per-seat pressure scores). MVP per the proposal restricts the
deterministic fields to `co_claims` (extracted from text) and `silent_seats`
(alive set minus speakers); `stances` / `pressure` / `open_topics` remain
skeletons and are passed through empty.

This module produces a `LogicPacket` that:

* enumerates the recipient's seat-aware view of CO claims as candidate
  entries (one per CO, each empty support/counter list — the NPC bot uses
  its own persona+role to weight them);
* echoes `silent_seats` as a textual `public_state_summary`;
* sets `expires_at_ms` per the current phase deadline;
* leaves `pressure` empty.

Builder is pure: no I/O, no asyncio. The arbiter passes in the deadline.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from wolfbot.domain.discussion import PublicDiscussionState
from wolfbot.domain.enums import Role
from wolfbot.domain.ws_messages import LogicCandidate, LogicPacket, RecentSpeech
from wolfbot.master.claim.claim_history import (
    ClaimHistory,
    expected_seer_claim_count_for_day,
)


def _new_packet_id() -> str:
    return f"lp_{uuid.uuid4().hex[:12]}"


def build_logic_packet(
    *,
    state: PublicDiscussionState,
    recipient_npc_id: str,
    expires_at_ms: int,
    now_ms: int,
    pressure: dict[int, float] | None = None,
    additional_candidates: Iterable[LogicCandidate] = (),
    recent_speeches: Iterable[RecentSpeech] = (),
    past_votes: Iterable[
        tuple[int, int, tuple[tuple[int, int | None], ...]]
    ] = (),
    past_suspicions: Iterable[
        tuple[int, str, int, int, str, str, str | None, str | None]
    ] = (),
    seat_names: dict[int, str] | None = None,
    claim_history: ClaimHistory | None = None,
    recipient_seat_no: int | None = None,
    recipient_role: Role | None = None,
) -> LogicPacket:
    """Construct a `LogicPacket` for `recipient_npc_id`.

    The packet is deterministic given the same `state` + `now_ms` save for
    the random `packet_id`. Tests should pin `now_ms` and inspect the rest of
    the payload directly.

    ``seat_names`` is a seat → display_name lookup so the rendered
    summary can refer to players by name instead of ``席N``. Optional
    for back-compat (older callers / tests pass nothing and get the
    legacy seat-only rendering); production callers in
    `SpeakArbiter.dispatch_request` always pass it.
    """
    name_map = seat_names or {}

    def _name(seat: int) -> str:
        return name_map.get(seat) or f"席{seat}"

    candidates: list[LogicCandidate] = []
    for claim in state.co_claims:
        candidates.append(
            LogicCandidate(
                id=f"co-{claim.seat}-{claim.role_claim}",
                claim=f"{_name(claim.seat)} {claim.role_claim}CO",
            )
        )
    candidates.extend(additional_candidates)

    silent_names = (
        "、".join(_name(s) for s in sorted(state.silent_seats))
        if state.silent_seats
        else ""
    )
    silent_repr = (
        f"silent_seats=[{silent_names}]" if silent_names else "silent_seats=[]"
    )
    co_repr = (
        ", ".join(f"{_name(c.seat)}={c.role_claim}" for c in state.co_claims)
        if state.co_claims
        else "(none)"
    )
    summary = f"phase_id={state.phase_id} day={state.day} co_claims=[{co_repr}] {silent_repr}"
    if state.pending_role_callouts:
        # Outstanding "誰か占い師?" / "霊媒師の方どうぞ" requests that no
        # one has answered yet. Real role holders should treat this as a
        # CO trigger; wolf-side NPCs should consider whether to fake CO.
        callouts_repr = ", ".join(sorted(state.pending_role_callouts))
        summary += f" pending_role_callouts=[{callouts_repr}]"
    if state.pending_co_response:
        # First-CO counter-CO window: a role just got its first claim
        # and every uncommitted wolf-side seat (plus the real role-holder
        # when the CO'er was wolf-side) is being rotated through the
        # priority pool. NPCs in the pool see this as "you're being asked
        # now; either counter-CO or skip — the window expires once
        # everyone has been asked".
        co_response_repr = ", ".join(sorted(state.pending_co_response))
        summary += f" pending_co_response=[{co_response_repr}]"
    if claim_history is not None and claim_history.by_seat:
        # Public per-claimer divination/medium history. Every NPC sees
        # the same record — real roles use it to keep their own past
        # results consistent in speech, fake-CO wolves see their own
        # prior lies and either commit to them or get caught when they
        # contradict themselves.
        #
        # Layout (post-game ``74edf214638d`` redesign):
        #
        # 1. Top warning banner: dead-claimer COs are still valid public
        #    info. Game ``74edf214638d`` had ステラ (madman) seer-CO on
        #    day 1, get attacked night 1, then on day 2 morning every
        #    NPC ignored her CO and called ユリコ "the single seer CO" —
        #    the LLM was treating dead = irrelevant. Banner up top is
        #    the cheapest fix; the rule also goes into prompt_builder.
        # 2. Per-role section header (### 占いCO / ### 霊媒CO) so the
        #    LLM doesn't confuse seer vs medium when scanning a row.
        # 3. Each row carries an explicit alive/dead tag in-line so the
        #    LLM doesn't need to cross-reference the participant list
        #    to know whether a claimer is still around.
        # 4. Distinct-claimer count vs cap is shown next to the role
        #    header (max 3 seer / 2 medium per CLAUDE rules). Per-day
        #    expected count (= N results per real seer by day-N morning,
        #    one per declared day) stays as a separate sub-line under
        #    the seer header.
        expected = expected_seer_claim_count_for_day(state.day)
        seer_claimers = sorted(
            s for s, h in claim_history.by_seat.items() if h.seer_claims
        )
        medium_claimers = sorted(
            s for s, h in claim_history.by_seat.items() if h.medium_claims
        )

        def _alive_tag(seat: int) -> str:
            return "生存" if seat in state.alive_seat_nos else "死亡"

        summary += "\n\n## 公開された占い/霊媒CO結果 (公式記録)\n"
        summary += (
            "**重要**: 死亡席の CO も依然として有効な公開情報です。"
            "alive/dead で ledger から除外しないでください。"
            "「占い師は◯◯さん 1 人だけ」「単独 CO」のように現在生存中の CO 数だけで"
            "結論を出すのは誤りで、必ず以下の通算件数で判断してください。\n"

        )
        if seer_claimers:
            summary += (
                f"\n### 占いCO  通算 {len(seer_claimers)} 件 / 上限 3 件 "
                f"(各人の発表は day{state.day} 朝までに通算 {expected} 件まで整合)\n"
            )
            for seat_no in seer_claimers:
                history = claim_history.by_seat[seat_no]
                who = _name(seat_no)
                seer_summary = ", ".join(
                    f"day{c.day}: {c.target_name}{'黒' if c.is_wolf else '白'}"
                    for c in history.seer_claims
                )
                summary += (
                    f"- {who} (席{seat_no}, {_alive_tag(seat_no)}) — {seer_summary}\n"
                )
        if medium_claimers:
            summary += (
                f"\n### 霊媒CO  通算 {len(medium_claimers)} 件 / 上限 2 件\n"
            )
            for seat_no in medium_claimers:
                history = claim_history.by_seat[seat_no]
                who = _name(seat_no)
                medium_summary = ", ".join(
                    f"day{c.day}: "
                    + (
                        f"{c.target_name}"
                        + ("黒" if c.is_wolf is True else "白" if c.is_wolf is False else "結果なし")
                    )
                    for c in history.medium_claims
                )
                summary += (
                    f"- {who} (席{seat_no}, {_alive_tag(seat_no)}) — {medium_summary}\n"
                )
        summary = summary.rstrip()
        # Per-recipient nudge when the addressee themselves is a CO'd
        # claimer whose announced result count lags the day-N expected
        # count. Without this, observation games (a51615d32274 day 2
        # ユリコ, 100b9e88e75a day 2 シゲミチ) showed the wolf seer
        # repeating the day-1 result instead of producing the night-1
        # result on day-2 morning, even though the count rule lives in
        # the system prompt — the LLM consistently failed to act on
        # the gap. Surface it here as a direct "this applies to YOU"
        # instruction so the model can't deflect to general advice.
        if (
            recipient_seat_no is not None
            and recipient_seat_no in claim_history.by_seat
            and state.day >= 1
        ):
            recipient_history = claim_history.by_seat[recipient_seat_no]
            recipient_seer_count = len(recipient_history.seer_claims)
            if (
                recipient_seer_count > 0
                and recipient_seer_count < expected
            ):
                missing = expected - recipient_seer_count
                # Past-night index in the ledger convention: at day-N
                # morning the most recent night is NIGHT_(N-1) (NIGHT_0
                # is the night before day 1, NIGHT_(N-1) is the night
                # before day N). `prev_night = state.day - 1` is the
                # one whose result is announced *today*.
                prev_night = max(0, state.day - 1)
                summary += (
                    f"\n\n## 【あなた宛 / 緊急】占いCO 結果の発表が不足しています\n"
                    f"あなたは占いCO 者で、現在の発表結果は通算 "
                    f"{recipient_seer_count} 件。day{state.day} 朝までの"
                    f"期待値は {expected} 件 (day1 朝で 1 件、以後毎朝 +1 件)。"
                    f"未発表が {missing} 件あります。"
                    f"**この発話で前夜 (NIGHT_{prev_night}) の新しい占い結果を必ず発表し、"
                    f"`claimed_seer_result` に `{{target_seat, is_wolf}}` を構造化"
                    f"して入れてください**。"
                    f"過去の結果 ({', '.join(c.target_name for c in recipient_history.seer_claims)}) "
                    f"を再表明するだけでは整合しません。"
                    f"対象は NIGHT_{prev_night} の開始時点で生存していた相手から選ぶこと。"
                )
            # Same gating logic for medium claimers: if the seat has
            # CO'd as medium and yesterday's execution exists in the
            # public log, they should produce yesterday's medium
            # result. We don't have a clean execution-count signal in
            # this packet (the audit games haven't shown medium drift
            # at the same severity yet), so a softer cumulative-count
            # instruction is enough — refining when we observe the
            # failure mode in real games.
            recipient_medium_count = len(recipient_history.medium_claims)
            if recipient_medium_count > 0 and state.day >= 2:
                summary += (
                    f"\n\n## 【あなた宛】霊媒CO 結果の発表確認\n"
                    f"あなたは霊媒CO 者で、現在の発表結果は通算 "
                    f"{recipient_medium_count} 件。day{state.day} 朝の時点で、"
                    f"昨日 (day{state.day - 1}) の処刑があった場合は"
                    f"その霊媒結果を `claimed_medium_result` に入れて発表する義務があります。"
                    f"処刑がなかった日は `is_wolf=null` で「結果なし」を明言してください。"
                )

    # Wolf-side counter-CO / fake-CO decision prompt. Fires when:
    #   (a) recipient is WEREWOLF or MADMAN
    #   (b) recipient hasn't CO'd as any info role yet
    #   (c) an info-role callout is active (pending_role_callouts /
    #       pending_co_response carries seer / medium / knight)
    # Surfaces the decision explicitly so the LLM treats this turn as a
    # binary "fake CO or skip" rather than drifting into generic chat.
    # The choice is the LLM's; this block frames the prompt.
    if recipient_role in (Role.WEREWOLF, Role.MADMAN) and recipient_seat_no is not None:
        recipient_co_keys: set[str] = set()
        if claim_history is not None and recipient_seat_no in claim_history.by_seat:
            history = claim_history.by_seat[recipient_seat_no]
            if history.seer_claims:
                recipient_co_keys.add("seer")
            if history.medium_claims:
                recipient_co_keys.add("medium")
        # Also check live state.co_claims (covers the in-phase case
        # where claim_history is built up to the current packet but
        # the brand-new CO this turn may not be folded yet).
        for c in state.co_claims:
            if c.seat == recipient_seat_no:
                recipient_co_keys.add(c.role_claim)
        already_co_info = bool(
            recipient_co_keys & {"seer", "medium", "knight"}
        )
        if not already_co_info:
            active_callouts: set[str] = set()
            for k in ("seer", "medium", "knight"):
                if (
                    k in state.pending_role_callouts
                    or k in state.pending_co_response
                ):
                    active_callouts.add(k)
            if "info_request" in state.pending_role_callouts:
                active_callouts |= {"seer", "medium", "knight"}
            for k in tuple(active_callouts):
                if any(c.role_claim == k for c in state.co_claims):
                    # Real or fake CO landed for this role at the same
                    # rough time as the prompt is being built — only
                    # surface a counter-CO decision when the role is
                    # genuinely unanswered. Drop already-answered
                    # role keys from the active set.
                    pass
            if active_callouts:
                role_label = {
                    "seer": "占い師",
                    "medium": "霊媒師",
                    "knight": "騎士",
                }
                roles_jp = "・".join(role_label[k] for k in sorted(active_callouts))
                # Per-role tactical hints — short reminders pulled from
                # the existing strategy templates so the wolf-side LLM
                # has the day-2+ medium / seer fake-CO playbook in
                # mind without re-fetching the whole role-strategy
                # block.
                hints: list[str] = []
                if "medium" in active_callouts and state.day >= 2:
                    hints.append(
                        "霊媒騙りを選ぶなら、`co_declaration=\"medium\"` を立てた上で、"
                        "前日処刑者への霊媒結果 (`claimed_medium_result`) を必ず添える "
                        "(処刑が無い日は `is_wolf=null` で結果なしを明言)。"
                    )
                if "seer" in active_callouts:
                    hints.append(
                        "占い騙りを選ぶなら、`co_declaration=\"seer\"` を立てた上で、"
                        "占い結果 (`claimed_seer_result`) を必ず添える。"
                        "day 1 朝なら NIGHT_0 ランダム白で必ず白判定、"
                        "day 2 以降なら前夜の判定 (黒も可) を出す。"
                        "対抗 CO がすでに出ている場合は対抗 ledger と矛盾しない対象を選ぶ。"
                    )
                if "knight" in active_callouts:
                    hints.append(
                        "騎士騙りを選ぶなら、`co_declaration=\"knight\"` を立て、"
                        "合法な護衛履歴 (自己護衛・連続護衛・死亡対象護衛・存在しない護衛成功は禁止) のみ提示する。"
                    )
                hint_block = ("\n  - " + "\n  - ".join(hints)) if hints else ""
                summary += (
                    f"\n\n## 【あなた宛 / 緊急判断】対抗 CO のチャンス\n"
                    f"今ターン {roles_jp} の CO が公開ログに登場しているか、誰かが該当役の名乗りを促しています。"
                    f"あなたは人狼陣営側 ({recipient_role.value}) で、まだ占い/霊媒/騎士のいずれにも CO していません。"
                    f"**この発話で {roles_jp} のいずれかとして対抗/騙り CO を出すか、それとも潜伏 (skip / 一般発話) を選ぶか** を判断してください。"
                    f"\n\n判断軸:"
                    f"\n  - 真役職者が単独で残ると村側が真寄りに固定する → 騙り CO で割って真偽比較を村に持ち込む価値"
                    f"\n  - 縄数 / 残狼数 / 既出 CO 件数を見て、騙りを増やすことで処刑筋がどう変わるか"
                    f"\n  - 騙ると「結果整合性 (発表内容の不変性)」を抱える義務が生じ、後の破綻リスクが上がる"
                    f"\n  - 潜伏が強い盤面 (例: 占い CO がすでに 3 件出ているなど) では skip も選択肢"
                    f"{hint_block}"
                    f"\n\n決定したら、CO する場合は `co_declaration` を該当役にセットし、`public_message` で自然な日本語で名乗る。"
                    f"潜伏する場合は `co_declaration=null` のままで、議論への一般的な反応・疑い表明・話題提示などを行う。"
                    f"\"何もせず黙る\" のは情報漏洩こそ無いが、村側が CO 群を絞る時間を与える消極策である点に注意。"
                )
    # Prefer the multi-addressee set; fall back to the legacy singular
    # field for state objects that haven't been migrated (e.g. test
    # fixtures that only set `last_addressed_seat`).
    addressed_seats: frozenset[int] = state.last_addressed_seats
    if not addressed_seats and state.last_addressed_seat is not None:
        addressed_seats = frozenset({state.last_addressed_seat})
    if addressed_seats:
        speaker_repr = (
            _name(state.last_addressed_speaker_seat)
            if state.last_addressed_speaker_seat is not None
            else "human"
        )
        # Truncate the spoken line so the packet stays small even if the
        # speaker rambled. NPCs only need the gist to respond on-topic.
        utter = state.last_addressed_text.strip().replace("\n", " ")
        if len(utter) > 160:
            utter = utter[:160] + "…"
        sorted_seats = sorted(addressed_seats)
        addr_repr = (
            _name(sorted_seats[0])
            if len(sorted_seats) == 1
            else "[" + "、".join(_name(s) for s in sorted_seats) + "]"
        )
        summary += (
            f" last_address={addr_repr}"
            f" from={speaker_repr} text=\"{utter}\""
        )

    return LogicPacket(
        ts=now_ms,
        trace_id=f"lp-{state.phase_id}-{recipient_npc_id}",
        packet_id=_new_packet_id(),
        phase_id=state.phase_id,
        recipient_npc_id=recipient_npc_id,
        public_state_summary=summary,
        logic_candidates=tuple(candidates),
        pressure=pressure or {},
        expires_at_ms=expires_at_ms,
        recent_speeches=tuple(recent_speeches),
        past_votes=tuple(past_votes),
        pending_role_callouts=tuple(sorted(state.pending_role_callouts)),
        past_suspicions=tuple(past_suspicions),
    )


__all__ = ["build_logic_packet"]
