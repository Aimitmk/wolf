"""Master-side validator for `claimed_seer_result` / `claimed_medium_result`.

Why
---
Reactive_voice mode lets each NPC bot generate its own structured CO
result. With a low-thinking model (e.g. ``gemini-2.5-flash`` +
``thinking_budget=0``) the model occasionally fabricates a divination
target that doesn't appear in its own private record (game
``ba084ae208cc`` Setsu, ``101d9a90ab58`` Gina). The fabricated claim
poisons the public claim ledger every subsequent prompt sees.

This module implements a pure, offline validator the arbiter calls
between ``handle_speak_result`` and ``PlaybackAuthorized``. If the
claim is structurally impossible (real seer claiming an unrecorded
target, fake CO swapping its own past target/color, day-1 morning
2nd seer claim, day-1 morning medium claim, etc.), the validator
returns a rejection reason + a feedback string. The caller drops
the playback (``PlaybackRejected``) and re-dispatches the same NPC
with the feedback embedded in the next prompt.

Inputs are passed as plain dataclasses so the unit tests don't
need to spin up a SqliteRepo.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.ws_messages import ClaimedMediumResult, ClaimedSeerResult
from wolfbot.master.claim_history import (
    ClaimedMediumEntry,
    ClaimedSeerEntry,
    ClaimerHistory,
)

# Rejection reason codes (also used as `failure_reason` on
# ``npc_speak_results``). Keep stable — they're observable.
REASON_SEER_FABRICATED_TARGET = "fabricated_seer_target"
REASON_SEER_WRONG_VERDICT = "fabricated_seer_verdict"
REASON_SEER_DAY1_OVERFLOW = "day1_seer_claim_overflow"
REASON_SEER_TARGET_SWAP = "seer_target_swap"
REASON_SEER_VERDICT_FLIP = "seer_verdict_flip"
REASON_SEER_CO_CAP_EXCEEDED = "seer_co_cap_exceeded"
REASON_MEDIUM_FABRICATED_TARGET = "fabricated_medium_target"
REASON_MEDIUM_WRONG_VERDICT = "fabricated_medium_verdict"
REASON_MEDIUM_DAY1 = "day1_medium_claim"
REASON_MEDIUM_NO_EXECUTION = "medium_claim_without_execution"
REASON_MEDIUM_TARGET_SWAP = "medium_target_swap"
REASON_MEDIUM_VERDICT_FLIP = "medium_verdict_flip"
REASON_MEDIUM_CO_CAP_EXCEEDED = "medium_co_cap_exceeded"

# Maximum number of distinct claimers per CO role. Beyond this, a fresh
# CO from a not-yet-CO'd seat is structurally fabricated:
#   * seer  — 真 1 + 狼 2 + 狂人 1 = 4 theoretical max, but a 4th CO can
#              only come from a non-roleholder villager and is immediately
#              cut by the village ledger.
#   * medium — 真 1 + 狼/狂人 1 騙り = 2 theoretical max; 3rd is broken.
#   * knight — same shape as medium.
SEER_CO_CAP = 3
MEDIUM_CO_CAP = 2

FABRICATION_REASONS = frozenset(
    {
        REASON_SEER_FABRICATED_TARGET,
        REASON_SEER_WRONG_VERDICT,
        REASON_SEER_DAY1_OVERFLOW,
        REASON_SEER_TARGET_SWAP,
        REASON_SEER_VERDICT_FLIP,
        REASON_MEDIUM_FABRICATED_TARGET,
        REASON_MEDIUM_WRONG_VERDICT,
        REASON_MEDIUM_DAY1,
        REASON_MEDIUM_NO_EXECUTION,
        REASON_MEDIUM_TARGET_SWAP,
        REASON_MEDIUM_VERDICT_FLIP,
    }
)

# CO-cap rejection reasons are NOT in FABRICATION_REASONS by design.
# Fabrication rejections trigger the same-NPC retry loop with feedback;
# the cap is a structural rule (= "you literally cannot CO this role
# anymore, no amount of self-correction will fix it"), so the right
# response is a one-time soft skip — drop this dispatch's audio, let
# the picker rotate to the next NPC, and don't burn retries on a
# speaker that the model can't help fixing.
CO_CAP_REASONS = frozenset(
    {REASON_SEER_CO_CAP_EXCEEDED, REASON_MEDIUM_CO_CAP_EXCEEDED}
)


@dataclass(frozen=True)
class ActualSeerEvent:
    """One real seer divination, derived from ``night_actions`` +
    target's ``seats.role``. Used only when the speaker IS the real
    seer; otherwise this list is empty."""

    day: int
    target_seat: int
    is_wolf: bool


@dataclass(frozen=True)
class ActualMediumEvent:
    """One real medium result, derived from public execution events
    + executed seat's ``seats.role``. Medium has no per-night action
    submission, so we synthesize the implied result from the prior
    day's execution. Used only when the speaker IS the real medium."""

    day: int  # day on which the execution log entry was emitted
    target_seat: int
    is_wolf: bool


@dataclass(frozen=True)
class ValidationResult:
    """Pure-function output: ok flag + machine reason + human feedback."""

    ok: bool
    reason: str | None = None
    feedback: str | None = None

    @classmethod
    def accept(cls) -> ValidationResult:
        return cls(ok=True)

    @classmethod
    def reject(cls, *, reason: str, feedback: str) -> ValidationResult:
        return cls(ok=False, reason=reason, feedback=feedback)


def _seer_history_block(history: Sequence[ActualSeerEvent]) -> str:
    if not history:
        return "(占い結果なし)"
    return "\n".join(
        f"  - day{ev.day}: 席{ev.target_seat} ({'黒' if ev.is_wolf else '白'})" for ev in history
    )


def _prior_seer_block(claims: Sequence[ClaimedSeerEntry]) -> str:
    if not claims:
        return "(過去の主張なし)"
    return "\n".join(
        f"  - day{c.day}: 席{c.target_seat} {c.target_name} ({'黒' if c.is_wolf else '白'})"
        for c in claims
    )


def _validate_seer_real(
    *,
    claim: ClaimedSeerResult,
    actual: Sequence[ActualSeerEvent],
) -> ValidationResult:
    """Real seer: claim must match an entry in ``actual`` exactly."""
    for ev in actual:
        if ev.target_seat == claim.target_seat:
            if ev.is_wolf == claim.is_wolf:
                return ValidationResult.accept()
            # Right target, wrong color: structurally impossible.
            return ValidationResult.reject(
                reason=REASON_SEER_WRONG_VERDICT,
                feedback=(
                    "前回の発話の `claimed_seer_result` は、"
                    f"対象 席{claim.target_seat} の判定色を"
                    f" {'黒' if claim.is_wolf else '白'} と主張したが、"
                    "あなたの非公開占い履歴では同じ対象の判定色は逆である。"
                    "本物の seer は判定色を後から塗り替えられない。"
                    "次の発話では、自分の `自分の占い結果` セクションに記録された"
                    "対象・色そのままで `claimed_seer_result` を埋めるか、"
                    "新しい結果を発表しないなら `claimed_seer_result=null` にする。"
                ),
            )
    return ValidationResult.reject(
        reason=REASON_SEER_FABRICATED_TARGET,
        feedback=(
            "前回の発話で `claimed_seer_result.target_seat="
            f"{claim.target_seat}` と主張したが、あなたの非公開占い履歴に"
            "その対象の記録は存在しない。本物の seer は記録外の対象を"
            "占ったと主張できない。実履歴は次の通り:\n"
            f"{_seer_history_block(actual)}\n"
            "次の発話では、上記のいずれかの対象+色をそのまま `claimed_seer_result` に"
            "入れるか、新しい結果を発表しない発話なら `claimed_seer_result=null` にする。"
        ),
    )


def _validate_seer_fake(
    *,
    claim: ClaimedSeerResult,
    day: int,
    phase: Phase,
    prior_self_claims: Sequence[ClaimedSeerEntry],
) -> ValidationResult:
    """Fake seer (wolf / madman / villager bluff): can invent targets,
    but must not retroactively swap or color-flip a same-night claim,
    and must not exceed day-1-morning's 1-claim cap."""
    # Day-1 morning overflow: a real seer at day=1 morning has exactly
    # 1 claim (NIGHT_0 random white). A fake seer who already issued
    # 1 claim cannot issue a 2nd same-morning.
    if day == 1 and phase is Phase.DAY_DISCUSSION:
        already_today = [c for c in prior_self_claims if c.day == 1]
        if already_today:
            return ValidationResult.reject(
                reason=REASON_SEER_DAY1_OVERFLOW,
                feedback=(
                    "day 1 朝に占い結果を主張できるのは NIGHT_0 のランダム白 1 件だけ。"
                    f"あなたは既にこのターン以前に day 1 で席{already_today[0].target_seat}"
                    f"の判定 ({'黒' if already_today[0].is_wolf else '白'}) を公表している。"
                    "本物の seer は同じ朝に 2 件目の判定を出せないため、"
                    "次の発話では `claimed_seer_result=null` にする (発表する新しい結果はない)。"
                ),
            )

    # Same-night swap / verdict flip: if this same speaker previously
    # claimed a different target for the same night (= same `day` value
    # in the seer-history convention: day-0 random + day-N nightly), reject.
    # The "night identity" rule: real seer claims at most 1 row per day.
    same_day_priors = [c for c in prior_self_claims if c.day == day]
    for prior in same_day_priors:
        if prior.target_seat != claim.target_seat:
            return ValidationResult.reject(
                reason=REASON_SEER_TARGET_SWAP,
                feedback=(
                    f"あなたは day{prior.day} の占い結果として既に"
                    f"席{prior.target_seat} {prior.target_name}を公表している"
                    f" ({'黒' if prior.is_wolf else '白'})。"
                    f"今回の `claimed_seer_result.target_seat={claim.target_seat}` は"
                    "同じ夜について別対象に差し替えており、本物の seer には起き得ない。"
                    "次の発話では、過去に公表した対象・色をそのまま使うか、"
                    "`claimed_seer_result=null` にする。"
                ),
            )
        if prior.is_wolf != claim.is_wolf:
            return ValidationResult.reject(
                reason=REASON_SEER_VERDICT_FLIP,
                feedback=(
                    f"あなたは day{prior.day} 席{prior.target_seat} の判定を既に"
                    f" {'黒' if prior.is_wolf else '白'} と公表している。"
                    "今回は同じ対象の判定色を逆に主張しており、本物の seer には起き得ない。"
                    "次の発話では過去発表通りに揃えるか、`claimed_seer_result=null` にする。"
                ),
            )
    return ValidationResult.accept()


def _validate_medium_real(
    *,
    claim: ClaimedMediumResult,
    actual: Sequence[ActualMediumEvent],
) -> ValidationResult:
    """Real medium: `target_seat` must match a recorded execution and
    the verdict must match the executed seat's actual role."""
    if claim.is_wolf is None:
        # Real medium emitting "no result" — only legal when there's
        # no execution recorded for the day this morning would surface
        # (i.e. yesterday had no execution). We can't perfectly detect
        # that here without the execution day, so accept the null verdict
        # but still validate the target_seat absence/presence loosely:
        # if there IS a real history entry that matches target_seat,
        # the model should not also be saying "no result".
        for ev in actual:
            if ev.target_seat == claim.target_seat:
                return ValidationResult.reject(
                    reason=REASON_MEDIUM_WRONG_VERDICT,
                    feedback=(
                        f"前回の発話で `claimed_medium_result.target_seat="
                        f"{claim.target_seat}` を主張しつつ `is_wolf=null` "
                        "(結果なし) としているが、その対象は実際に処刑されており"
                        f"判定色は {'黒' if ev.is_wolf else '白'} のはず。"
                        "次の発話では、結果を発表する場合は実履歴通りの対象+色で、"
                        "発表しない場合は `claimed_medium_result=null` にする。"
                    ),
                )
        return ValidationResult.accept()
    for ev in actual:
        if ev.target_seat == claim.target_seat:
            if ev.is_wolf == claim.is_wolf:
                return ValidationResult.accept()
            return ValidationResult.reject(
                reason=REASON_MEDIUM_WRONG_VERDICT,
                feedback=(
                    f"前回の発話の `claimed_medium_result` は、"
                    f"対象 席{claim.target_seat} の判定色を"
                    f" {'黒' if claim.is_wolf else '白'} と主張したが、"
                    "あなたの非公開霊媒履歴では同じ対象の判定色は逆である。"
                    "次の発話では実履歴通りに揃えるか、"
                    "`claimed_medium_result=null` にする。"
                ),
            )
    return ValidationResult.reject(
        reason=REASON_MEDIUM_FABRICATED_TARGET,
        feedback=(
            f"前回の発話で `claimed_medium_result.target_seat="
            f"{claim.target_seat}` と主張したが、あなたの非公開霊媒履歴に"
            "その対象の記録は存在しない (=その席は処刑されていない)。"
            "本物の medium は処刑されていない席を霊媒結果として出せない。"
            "次の発話では実履歴通りに揃えるか、`claimed_medium_result=null` にする。"
        ),
    )


def _validate_medium_fake(
    *,
    claim: ClaimedMediumResult,
    day: int,
    phase: Phase,
    prior_self_claims: Sequence[ClaimedMediumEntry],
    executions_so_far: int,
) -> ValidationResult:
    """Fake medium (any non-medium role): day-1 morning has no legal
    medium result; can't exceed execution count; can't swap own past."""
    if day == 1 and phase is Phase.DAY_DISCUSSION:
        return ValidationResult.reject(
            reason=REASON_MEDIUM_DAY1,
            feedback=(
                "day 1 朝の霊媒結果は構造的に存在しない (前日の処刑がまだない)。"
                "本物の medium は day 1 朝に結果を出せないため、"
                "`claimed_medium_result=null` にする。"
                "霊媒師 CO 自体は day 1 朝でも可能だが、結果は伴わない。"
            ),
        )
    if executions_so_far == 0 and claim.is_wolf is not None:
        return ValidationResult.reject(
            reason=REASON_MEDIUM_NO_EXECUTION,
            feedback=(
                "まだ処刑が一度も発生していないため、霊媒結果を出せる対象が存在しない。"
                "本物の medium は処刑がない時点で結果を発表しない。"
                "次の発話では `claimed_medium_result=null` にする。"
            ),
        )
    same_day_priors = [c for c in prior_self_claims if c.day == day]
    for prior in same_day_priors:
        if prior.target_seat != claim.target_seat:
            return ValidationResult.reject(
                reason=REASON_MEDIUM_TARGET_SWAP,
                feedback=(
                    f"あなたは day{prior.day} の霊媒結果として既に"
                    f"席{prior.target_seat} {prior.target_name}を公表している。"
                    "今回は同じ夜について別対象に差し替えており、"
                    "本物の medium には起き得ない。"
                    "次の発話では過去発表通りに揃えるか、"
                    "`claimed_medium_result=null` にする。"
                ),
            )
        if (
            prior.is_wolf is not None
            and claim.is_wolf is not None
            and prior.is_wolf != claim.is_wolf
        ):
            return ValidationResult.reject(
                reason=REASON_MEDIUM_VERDICT_FLIP,
                feedback=(
                    f"あなたは day{prior.day} 席{prior.target_seat} の霊媒結果を既に"
                    f" {'黒' if prior.is_wolf else '白'} と公表している。"
                    "今回は同じ対象の判定色を逆に主張しており、本物の medium には起き得ない。"
                    "次の発話では過去発表通りに揃えるか、`claimed_medium_result=null` にする。"
                ),
            )
    return ValidationResult.accept()


def validate_claim_against_truth(
    *,
    speaker_role: Role,
    speaker_seat: int,
    day: int,
    phase: Phase,
    claimed_seer: ClaimedSeerResult | None,
    claimed_medium: ClaimedMediumResult | None,
    actual_seer_history: Sequence[ActualSeerEvent] = (),
    actual_medium_history: Sequence[ActualMediumEvent] = (),
    prior_public_claims: ClaimerHistory | None = None,
    executions_so_far: int = 0,
    seer_co_count: int = 0,
    medium_co_count: int = 0,
) -> ValidationResult:
    """Single entry point: validate this utterance's structured claims.

    Returns ``ValidationResult.accept()`` when the claims are internally
    consistent. Returns a rejection with a machine reason code (one of
    ``FABRICATION_REASONS``) plus a Japanese feedback string suitable
    for embedding in the next ``SpeakRequest.retry_feedback``.

    Both ``claimed_seer`` and ``claimed_medium`` may be ``None`` (the
    common case when the utterance doesn't announce a new result);
    that's always accepted.

    ``seer_co_count`` / ``medium_co_count`` are the count of *distinct
    seats* that have already issued a CO of that role in the public
    ledger. When this seat is brand-new to the role (no prior claim
    of their own) AND the count is already at the structural cap
    (3 for seer, 2 for medium), the new CO is rejected as
    fabrication. Game ``59d5377c6794`` reproduced this: a 4th seer
    CO appeared (seat 6 ジョナス) on day 1 explicitly noting "占い師
    が三人も揃うとはね、私こそが導き手" and the prompt-side
    rule didn't stop it — only a hard validator catches it.
    """
    prior_seer = prior_public_claims.seer_claims if prior_public_claims else ()
    prior_medium = prior_public_claims.medium_claims if prior_public_claims else ()

    if claimed_seer is not None:
        # CO cap check: applies BEFORE real/fake-specific rules. If the
        # ledger already has SEER_CO_CAP distinct claimers and this
        # speaker hasn't claimed yet, the CO is structurally fabricated
        # regardless of role (real seer would still fail this — but
        # `seer_co_count >= cap` while the real seer hasn't CO'd is
        # itself impossible because the real seer is one of the distinct
        # claimers; so this branch only fires for non-real CO inflation).
        if not prior_seer and seer_co_count >= SEER_CO_CAP:
            return ValidationResult.reject(
                reason=REASON_SEER_CO_CAP_EXCEEDED,
                feedback=(
                    f"前回の発話で占い師 CO を出したが、"
                    f"既に公開ログ上 {seer_co_count} 件の占い師 CO が出ている。"
                    f"占い師 CO は構造的に最大 {SEER_CO_CAP} 件 (真 1 + 狼 2 + 狂人 1) で、"
                    f"{SEER_CO_CAP + 1} 件目は村陣営の村人騙りとしてしか説明がつかず、"
                    "村側に強い狼/狂人ラインとして即座に切られる破綻行動である。"
                    "次の発話では占い師 CO に出ないこと "
                    "(`co_declaration=null`、`claimed_seer_result=null` を設定)。"
                    "潜伏のまま発話するか、別役職 (霊媒/騎士) の対抗 CO 余地を考える "
                    "(ただし霊媒/騎士も既に上限に達していないか確認すること)。"
                ),
            )
        if speaker_role is Role.SEER:
            res = _validate_seer_real(
                claim=claimed_seer,
                actual=actual_seer_history,
            )
            if not res.ok:
                return res
        else:
            res = _validate_seer_fake(
                claim=claimed_seer,
                day=day,
                phase=phase,
                prior_self_claims=prior_seer,
            )
            if not res.ok:
                return res

    if claimed_medium is not None:
        # Mirror the seer cap for medium (max 2 distinct claimers).
        if not prior_medium and medium_co_count >= MEDIUM_CO_CAP:
            return ValidationResult.reject(
                reason=REASON_MEDIUM_CO_CAP_EXCEEDED,
                feedback=(
                    f"前回の発話で霊媒師 CO を出したが、"
                    f"既に公開ログ上 {medium_co_count} 件の霊媒師 CO が出ている。"
                    f"霊媒師 CO は構造的に最大 {MEDIUM_CO_CAP} 件 (真 1 + 狼/狂人 1) で、"
                    f"{MEDIUM_CO_CAP + 1} 件目は本物の霊媒師ではあり得ない破綻行動である。"
                    "次の発話では霊媒師 CO に出ないこと "
                    "(`co_declaration=null`、`claimed_medium_result=null` を設定)。"
                ),
            )
        if speaker_role is Role.MEDIUM:
            res = _validate_medium_real(
                claim=claimed_medium,
                actual=actual_medium_history,
            )
            if not res.ok:
                return res
        else:
            res = _validate_medium_fake(
                claim=claimed_medium,
                day=day,
                phase=phase,
                prior_self_claims=prior_medium,
                executions_so_far=executions_so_far,
            )
            if not res.ok:
                return res

    return ValidationResult.accept()


__all__ = [
    "CO_CAP_REASONS",
    "FABRICATION_REASONS",
    "MEDIUM_CO_CAP",
    "REASON_MEDIUM_CO_CAP_EXCEEDED",
    "REASON_MEDIUM_DAY1",
    "REASON_MEDIUM_FABRICATED_TARGET",
    "REASON_MEDIUM_NO_EXECUTION",
    "REASON_MEDIUM_TARGET_SWAP",
    "REASON_MEDIUM_VERDICT_FLIP",
    "REASON_MEDIUM_WRONG_VERDICT",
    "REASON_SEER_CO_CAP_EXCEEDED",
    "REASON_SEER_DAY1_OVERFLOW",
    "REASON_SEER_FABRICATED_TARGET",
    "REASON_SEER_TARGET_SWAP",
    "REASON_SEER_VERDICT_FLIP",
    "REASON_SEER_WRONG_VERDICT",
    "SEER_CO_CAP",
    "ActualMediumEvent",
    "ActualSeerEvent",
    "ValidationResult",
    "validate_claim_against_truth",
]
