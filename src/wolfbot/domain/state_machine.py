"""Pure state-machine transitions.

Every function takes a game snapshot + rules inputs and returns a `Transition` describing
what should happen next. No I/O, no time calls — `now_epoch` is always explicit.

The transition is applied by `game_service.advance()` in this order:
  1. Discord channel permissions (idempotent).
  2. Public Discord announcements (for `public_logs` + `morning_text`).
  3. DM submissions for any newly needed action.
  4. SQLite commit via `SqliteRepo.apply_transition` (optimistic lock on expected_phase).

Phase durations for deadline computation:
  - DAY_DISCUSSION: 300 / 240 / 180 by `day_discussion_duration(day)`.
  - DAY_VOTE / DAY_RUNOFF / NIGHT: fixed constants below.
  - SETUP / NIGHT_0: no deadline (transient — engine iterates immediately).
  - WAITING_HOST_DECISION / GAME_OVER: no deadline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from random import Random

from wolfbot.domain.durations import current_phase_durations
from wolfbot.domain.enums import (
    FACTION_JA,
    ROLE_JA,
    DeathCause,
    Faction,
    Phase,
    Role,
    SubmissionType,
)
from wolfbot.domain.models import (
    AttackResult,
    Game,
    LogEntry,
    NightAction,
    PendingDecision,
    PendingSubmission,
    Player,
    PlayerUpdate,
    Seat,
    Transition,
    Vote,
)
from wolfbot.domain.rules import (
    alive_seat_of_role,
    alive_werewolves,
    check_victory,
    compute_vote_result,
    day_discussion_duration,
    is_detected_as_wolf,
    medium_detection,
    random_white_target,
    resolve_wolf_attack,
)

# Initial deadline for DAY_RUNOFF_SPEECH. Acts as a safety net so a hung LLM
# task doesn't freeze the game; the LLM dispatcher's per-seat `finally` block
# always advances `runoff_speech_done` so the engine can move on.
#
# Not part of :class:`PhaseDurations` because it's a hard safety floor rather
# than a tunable phase length — adjust here if real LLM submissions need more
# headroom, but don't expose it to the duration_factor knob.
RUNOFF_SPEECH_DEADLINE = 60


# Backwards-compatible re-exports of the historical ``int`` constants.
# Per :pep:`562`, this module-level ``__getattr__`` is consulted when a
# name is not found in the module's normal namespace, so
# ``state_machine.VOTE_DURATION`` returns the *current* singleton value
# at access time rather than a stale snapshot from import.
#
# Note: ``from wolfbot.domain.state_machine import VOTE_DURATION`` still
# binds the local name to the value resolved at import time, so any
# tests that rely on dynamic re-reading must access the constant via
# ``current_phase_durations()`` directly. The aliases here exist so
# (a) tests that assert against the default value keep working, and
# (b) any third-party caller that reads the module attribute gets the
# current value.
_DURATION_ALIASES: dict[str, str] = {
    "VOTE_DURATION": "vote",
    "RUNOFF_DURATION": "runoff",
    "NIGHT_DURATION": "night",
    "DAY_DISCUSSION_GRACE": "day_discussion_grace",
    "RUNOFF_SPEECH_GRACE": "runoff_speech_grace",
}


def __getattr__(name: str) -> int:
    field = _DURATION_ALIASES.get(name)
    if field is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value: int = getattr(current_phase_durations(), field)
    return value


# ---------------------------------------------------------------- helpers
def _name(seats: Mapping[int, Seat], seat_no: int) -> str:
    seat = seats.get(seat_no)
    if seat is None:
        return f"座席{seat_no}"
    return seat.display_name


def _public_log(
    game: Game,
    kind: str,
    text: str,
    now_epoch: int,
    phase: Phase | None = None,
    actor_seat: int | None = None,
    day: int | None = None,
) -> LogEntry:
    # `actor_seat` is forwarded so EXECUTION (target) and MORNING (victim)
    # carry the affected seat. Master Levi narration reads this field to
    # voice the actual seat label instead of "対象不明".
    #
    # ``day`` overrides the default ``game.day_number`` for transitions
    # that cross a day boundary in the same Transition (e.g. the NIGHT
    # resolver emits the next day's MORNING + "N 日目の議論を開始"
    # PHASE_CHANGE before bumping day_number). Without the override
    # those logs land in the *prior* day's bucket, and the viewer's
    # per-(day, phase) timeline reads "DAY_VOTE → NIGHT → DAY_DISCUSSION"
    # — the discussion section ends up dated AFTER its own speeches.
    return LogEntry(
        game_id=game.id,
        day=game.day_number if day is None else day,
        phase=phase or game.phase,
        kind=kind,
        actor_seat=actor_seat,
        visibility="PUBLIC",
        text=text,
        created_at=now_epoch,
    )


def _private_log(
    game: Game,
    audience_seat: int,
    kind: str,
    text: str,
    now_epoch: int,
    phase: Phase | None = None,
    day: int | None = None,
) -> LogEntry:
    return LogEntry(
        game_id=game.id,
        day=game.day_number if day is None else day,
        phase=phase or game.phase,
        kind=kind,
        actor_seat=None,
        visibility="PRIVATE",
        audience_seat=audience_seat,
        text=text,
        created_at=now_epoch,
    )


def _victory_log(game: Game, v: Faction, now_epoch: int) -> LogEntry:
    return _public_log(
        game,
        kind="VICTORY",
        text=f"ゲーム終了。勝利陣営: {FACTION_JA[v]}",
        now_epoch=now_epoch,
        phase=Phase.GAME_OVER,
    )


def _role_reveal_log(
    game: Game,
    players_after: Sequence[Player],
    seats_by_no: Mapping[int, Seat],
    now_epoch: int,
) -> LogEntry:
    """Final roster reveal, emitted to the main public channel on GAME_OVER.

    `players_after` must already reflect post-transition alive/dead state — caller
    is responsible for applying newly-dead seat flips before handing players in.
    """
    lines = ["最終配役:"]
    for p in sorted(players_after, key=lambda x: x.seat_no):
        role_ja = ROLE_JA[p.role] if p.role is not None else "?"
        status = "生存" if p.alive else "死亡"
        lines.append(f"- 席{p.seat_no} {_name(seats_by_no, p.seat_no)}: {role_ja} ({status})")
    return _public_log(
        game,
        kind="ROLE_REVEAL",
        text="\n".join(lines),
        now_epoch=now_epoch,
        phase=Phase.GAME_OVER,
    )


def _format_vote_results_by_voter(
    votes: Sequence[Vote],
    seats_by_no: Mapping[int, Seat],
    alive_seats: set[int],
) -> str:
    """Voter-keyed vote display, empty string if no valid ballots cast.

    Output:
        🗳 投票結果:
        ・<voter_name> -> <target_name>
        ・<voter_name> -> 棄権

    Sorted by voter seat_no ascending. Dead-seat stale votes filtered to match
    `compute_vote_result`. Defensive de-dup: if duplicate Vote rows exist for the
    same voter (the DB upsert prevents this in practice), keep the latest by
    `submitted_at`.
    """
    valid = [v for v in votes if v.voter_seat in alive_seats]
    if not valid:
        return ""
    by_voter: dict[int, Vote] = {}
    for v in valid:
        prev = by_voter.get(v.voter_seat)
        if prev is None or v.submitted_at >= prev.submitted_at:
            by_voter[v.voter_seat] = v
    lines = ["🗳 投票結果:"]
    for voter_seat in sorted(by_voter):
        v = by_voter[voter_seat]
        voter_name = _name(seats_by_no, voter_seat)
        target_label = "棄権" if v.target_seat is None else _name(seats_by_no, v.target_seat)
        lines.append(f"・{voter_name} -> {target_label}")
    return "\n".join(lines)


# ---------------------------------------------------------------- SETUP
def plan_setup(
    game: Game,
    seats: Sequence[Seat],
    rng: Random,
    now_epoch: int,
) -> Transition:
    """SETUP → NIGHT_0. Assigns roles; no deadline (transient)."""
    from wolfbot.domain.rules import assign_roles

    role_map = assign_roles(seats, rng)
    updates = tuple(PlayerUpdate(seat_no=sn, role=role) for sn, role in sorted(role_map.items()))
    pub = _public_log(
        game,
        kind="SETUP_COMPLETE",
        text="配役が決定しました。役職は DM をご確認ください。",
        now_epoch=now_epoch,
        phase=Phase.SETUP,
    )
    return Transition(
        next_phase=Phase.NIGHT_0,
        next_day=0,
        new_deadline_epoch=None,
        player_updates=updates,
        public_logs=(pub,),
    )


# ---------------------------------------------------------------- NIGHT_0
def plan_night0(
    game: Game,
    players: Sequence[Player],
    seats: Sequence[Seat],
    rng: Random,
    now_epoch: int,
) -> Transition:
    """NIGHT_0 work: role notices + seer random white + wolf partner intros → DAY_DISCUSSION day 1."""
    seats_by_no = {s.seat_no: s for s in seats}
    private: list[LogEntry] = []

    for p in players:
        if p.role is None:
            continue
        private.append(
            _private_log(
                game,
                audience_seat=p.seat_no,
                kind="ROLE_NOTICE",
                text=f"あなたの役職は『{ROLE_JA[p.role]}』です。",
                now_epoch=now_epoch,
                phase=Phase.NIGHT_0,
            )
        )

    # Seer random white
    seer_seat = alive_seat_of_role(players, Role.SEER)
    if seer_seat is not None:
        white_target = random_white_target(players, seer_seat, rng)
        private.append(
            _private_log(
                game,
                audience_seat=seer_seat,
                kind="SEER_RESULT_NIGHT0",
                text=(
                    f"初日ランダム白: {_name(seats_by_no, white_target)} は 人狼ではありません。"
                ),
                now_epoch=now_epoch,
                phase=Phase.NIGHT_0,
            )
        )

    # Wolf partner intros
    wolves = [p.seat_no for p in players if p.role is Role.WEREWOLF and p.alive]
    for w in wolves:
        partners = [s for s in wolves if s != w]
        partners_text = "、".join(_name(seats_by_no, s) for s in partners) or "(なし)"
        private.append(
            _private_log(
                game,
                audience_seat=w,
                kind="WOLF_PARTNER",
                text=f"あなたの相方: {partners_text}",
                now_epoch=now_epoch,
                phase=Phase.NIGHT_0,
            )
        )

    day1_start = _public_log(
        game,
        kind="PHASE_CHANGE",
        text=(
            "夜が明けました。1 日目の議論を開始します。"
            f"制限時間は {day_discussion_duration(1)} 秒です。"
        ),
        now_epoch=now_epoch,
        phase=Phase.DAY_DISCUSSION,
        day=1,
    )

    return Transition(
        next_phase=Phase.DAY_DISCUSSION,
        next_day=1,
        new_deadline_epoch=now_epoch + day_discussion_duration(1),
        public_logs=(day1_start,),
        private_logs=tuple(private),
    )


# ---------------------------------------------------------------- DAY_DISCUSSION → DAY_VOTE
def plan_day_discussion_to_vote(game: Game, now_epoch: int) -> Transition:
    """Discussion deadline hit → move to vote phase."""
    pub = _public_log(
        game,
        kind="PHASE_CHANGE",
        text="議論時間終了。投票フェイズを開始します。DM の投票 UI からお選びください。",
        now_epoch=now_epoch,
        phase=Phase.DAY_VOTE,
    )
    return Transition(
        next_phase=Phase.DAY_VOTE,
        next_day=game.day_number,
        new_deadline_epoch=now_epoch + current_phase_durations().vote,
        public_logs=(pub,),
    )


# ---------------------------------------------------------------- DAY_VOTE
def plan_day_vote_resolve(
    game: Game,
    players: Sequence[Player],
    seats: Sequence[Seat],
    votes: Sequence[Vote],
    force_skip: bool,
    now_epoch: int,
) -> Transition:
    alive_seats = {p.seat_no for p in players if p.alive}
    submitted = {v.voter_seat for v in votes if v.voter_seat in alive_seats}
    missing = tuple(sorted(alive_seats - submitted))

    if missing and not force_skip:
        return Transition(
            next_phase=Phase.WAITING_HOST_DECISION,
            next_day=game.day_number,
            new_deadline_epoch=None,
            requires_host_decision=True,
            pending=PendingDecision(
                game_id=game.id,
                phase=Phase.DAY_VOTE,
                day=game.day_number,
                required_submission=SubmissionType.VOTE,
                missing_seats=missing,
                submissions=(
                    PendingSubmission(
                        submission_type=SubmissionType.VOTE,
                        missing_seats=missing,
                    ),
                ),
                created_at=now_epoch,
            ),
        )

    outcome = compute_vote_result(votes, alive_seats=alive_seats)
    seats_by_no = {s.seat_no: s for s in seats}
    tally = _format_vote_results_by_voter(votes, seats_by_no, alive_seats)
    tally_suffix = f"\n\n{tally}" if tally else ""

    if outcome.executed is not None:
        return _apply_execution(
            game,
            players,
            seats_by_no,
            outcome.executed,
            now_epoch,
            clear_force=True,
            tally_suffix=tally_suffix,
        )
    if outcome.tied:
        candidates = "、".join(_name(seats_by_no, s) for s in outcome.tied)
        tied_has_llm = any(
            seats_by_no.get(sn) is not None and seats_by_no[sn].is_llm for sn in outcome.tied
        )
        if tied_has_llm:
            # Some tied candidate is an LLM seat. Park in DAY_RUNOFF_SPEECH so
            # the candidate LLMs each speak once before runoff voting begins.
            pub = _public_log(
                game,
                kind="RUNOFF_START",
                text=f"同票のため決選投票に移ります。候補: {candidates}{tally_suffix}",
                now_epoch=now_epoch,
                phase=Phase.DAY_RUNOFF_SPEECH,
            )
            return Transition(
                next_phase=Phase.DAY_RUNOFF_SPEECH,
                next_day=game.day_number,
                new_deadline_epoch=now_epoch + RUNOFF_SPEECH_DEADLINE,
                public_logs=(pub,),
                clear_force_skip=True,
            )
        pub = _public_log(
            game,
            kind="RUNOFF_START",
            text=f"同票のため決選投票に移ります。候補: {candidates}{tally_suffix}",
            now_epoch=now_epoch,
            phase=Phase.DAY_RUNOFF,
        )
        return Transition(
            next_phase=Phase.DAY_RUNOFF,
            next_day=game.day_number,
            new_deadline_epoch=now_epoch + current_phase_durations().runoff,
            public_logs=(pub,),
            clear_force_skip=True,
        )
    # all abstained / no valid votes → no execution
    pub = _public_log(
        game,
        kind="NO_EXECUTION",
        text=f"有効な投票がなかったため、本日は処刑なしで夜を迎えます。{tally_suffix}",
        now_epoch=now_epoch,
        phase=Phase.NIGHT,
    )
    return Transition(
        next_phase=Phase.NIGHT,
        next_day=game.day_number,
        new_deadline_epoch=now_epoch + current_phase_durations().night,
        public_logs=(pub,),
        clear_force_skip=True,
    )


# ---------------------------------------------------------------- DAY_RUNOFF
def plan_day_runoff_resolve(
    game: Game,
    players: Sequence[Player],
    seats: Sequence[Seat],
    votes: Sequence[Vote],
    tied_candidates: Sequence[int],
    force_skip: bool,
    now_epoch: int,
) -> Transition:
    alive_seats = {p.seat_no for p in players if p.alive}
    submitted = {v.voter_seat for v in votes if v.voter_seat in alive_seats}
    missing = tuple(sorted(alive_seats - submitted))

    if missing and not force_skip:
        return Transition(
            next_phase=Phase.WAITING_HOST_DECISION,
            next_day=game.day_number,
            new_deadline_epoch=None,
            requires_host_decision=True,
            pending=PendingDecision(
                game_id=game.id,
                phase=Phase.DAY_RUNOFF,
                day=game.day_number,
                required_submission=SubmissionType.RUNOFF_VOTE,
                missing_seats=missing,
                submissions=(
                    PendingSubmission(
                        submission_type=SubmissionType.RUNOFF_VOTE,
                        missing_seats=missing,
                    ),
                ),
                created_at=now_epoch,
            ),
        )

    outcome = compute_vote_result(
        votes, alive_seats=alive_seats, candidate_seats=set(tied_candidates)
    )
    seats_by_no = {s.seat_no: s for s in seats}
    tally = _format_vote_results_by_voter(votes, seats_by_no, alive_seats)
    tally_suffix = f"\n\n{tally}" if tally else ""

    if outcome.executed is not None:
        return _apply_execution(
            game,
            players,
            seats_by_no,
            outcome.executed,
            now_epoch,
            clear_force=True,
            tally_suffix=tally_suffix,
        )
    # Runoff tie → no execution
    pub = _public_log(
        game,
        kind="NO_EXECUTION",
        text=f"決選投票も同票のため、本日は処刑なしで夜を迎えます。{tally_suffix}",
        now_epoch=now_epoch,
        phase=Phase.NIGHT,
    )
    return Transition(
        next_phase=Phase.NIGHT,
        next_day=game.day_number,
        new_deadline_epoch=now_epoch + current_phase_durations().night,
        public_logs=(pub,),
        clear_force_skip=True,
    )


def _apply_execution(
    game: Game,
    players: Sequence[Player],
    seats_by_no: Mapping[int, Seat],
    executed_seat: int,
    now_epoch: int,
    *,
    clear_force: bool,
    tally_suffix: str = "",
) -> Transition:
    """Apply execution death, check victory, go to NIGHT or GAME_OVER."""
    # Defense-in-depth: compute_vote_result filters by alive seats, but if a
    # dead seat ever reaches here (corrupted DB, stale vote bypassing the
    # service-layer guard) refuse to overwrite death_day — treat as no-execution.
    if not any(p.seat_no == executed_seat and p.alive for p in players):
        pub = _public_log(
            game,
            kind="NO_EXECUTION",
            text=f"投票結果が無効だったため、本日は処刑なしで夜を迎えます。{tally_suffix}",
            now_epoch=now_epoch,
            phase=Phase.NIGHT,
        )
        return Transition(
            next_phase=Phase.NIGHT,
            next_day=game.day_number,
            new_deadline_epoch=now_epoch + current_phase_durations().night,
            public_logs=(pub,),
            clear_force_skip=clear_force,
        )
    exec_name = _name(seats_by_no, executed_seat)
    public_logs: tuple[LogEntry, ...] = (
        _public_log(
            game,
            kind="EXECUTION",
            text=f"{exec_name} が処刑されました。{tally_suffix}",
            now_epoch=now_epoch,
            actor_seat=executed_seat,
        ),
    )
    updates = (
        PlayerUpdate(
            seat_no=executed_seat,
            alive=False,
            death_cause=DeathCause.EXECUTION,
            death_day=game.day_number,
        ),
    )

    new_players = [
        p.model_copy(update={"alive": False}) if p.seat_no == executed_seat else p for p in players
    ]
    v = check_victory(new_players)
    if v is not None:
        return Transition(
            next_phase=Phase.GAME_OVER,
            next_day=game.day_number,
            new_deadline_epoch=None,
            player_updates=updates,
            public_logs=(
                *public_logs,
                _victory_log(game, v, now_epoch),
                _role_reveal_log(game, new_players, seats_by_no, now_epoch),
            ),
            victory=v,
            newly_dead_seats=(executed_seat,),
            clear_force_skip=clear_force,
        )
    return Transition(
        next_phase=Phase.NIGHT,
        next_day=game.day_number,
        new_deadline_epoch=now_epoch + current_phase_durations().night,
        player_updates=updates,
        public_logs=public_logs,
        newly_dead_seats=(executed_seat,),
        clear_force_skip=clear_force,
    )


# ---------------------------------------------------------------- NIGHT
def plan_night_resolve(
    game: Game,
    players: Sequence[Player],
    seats: Sequence[Seat],
    actions: Sequence[NightAction],
    previous_guard_seat: int | None,
    force_skip: bool,
    now_epoch: int,
    rng: Random | None = None,
) -> Transition:
    """Night resolution in the spec's fixed 10-step order.

    1. Medium result → 2. Seer result → 3. Guard target → 4. Attack target
    → 5. guard==attack? (no death) → 6. else attack target dies
    → 7. Morning announce → 8. Permission update (via newly_dead_seats)
    → 9. Victory check → 10. DAY_DISCUSSION day+1 (or GAME_OVER).

    ``rng`` is forwarded to :func:`wolfbot.domain.rules.resolve_wolf_attack`
    so an all-LLM split picks one wolf's target at random instead of
    parking the game in ``WAITING_HOST_DECISION``. ``None`` keeps the
    legacy split-pause behaviour for tests / callers that aren't yet
    threading an RNG.
    """
    seats_by_no = {s.seat_no: s for s in seats}
    seer_seat = alive_seat_of_role(players, Role.SEER)
    knight_seat = alive_seat_of_role(players, Role.KNIGHT)
    medium_seat = alive_seat_of_role(players, Role.MEDIUM)
    wolves = alive_werewolves(players)
    # Human-wolf priority: when 2 wolves disagree and one is human, the
    # human's pick wins (instead of marking the attack as split). Computed
    # here so `resolve_wolf_attack` stays domain-pure (no Seat dependency).
    human_wolf_seats = [
        w for w in wolves if seats_by_no.get(w) is not None and not seats_by_no[w].is_llm
    ]

    wolf_actions = [a for a in actions if a.kind is SubmissionType.WOLF_ATTACK]
    seer_action = next((a for a in actions if a.kind is SubmissionType.SEER_DIVINE), None)
    knight_action = next((a for a in actions if a.kind is SubmissionType.KNIGHT_GUARD), None)

    attack = resolve_wolf_attack(
        wolf_actions,
        wolves,
        force_skip=force_skip,
        human_wolf_seats=human_wolf_seats,
        rng=rng,
    )

    missing: set[int] = set()
    if seer_seat is not None and seer_action is None:
        missing.add(seer_seat)
    if knight_seat is not None and knight_action is None and game.day_number >= 1:
        missing.add(knight_seat)
    if attack.missing:
        missing.update(attack.missing)

    # Spec: "1 対 1 で割れた場合、締切時点では未確定のままとし、WAITING_HOST_DECISION に遷移する"
    # force_skip=True overrides — treat split as attack fail.
    wolves_split_pauses = attack.split and not force_skip

    if (missing or wolves_split_pauses) and not force_skip:
        wolf_missing_seats = tuple(sorted(set(missing) & set(wolves)))
        wolf_unresolved_seats = tuple(sorted(wolves)) if wolves_split_pauses else ()
        # `missing_seats` is the legacy primary summary (union of all seats that
        # need action — real no-submit plus split wolves that must re-pick).
        pending_missing = set(missing)
        if wolves_split_pauses:
            pending_missing.update(wolves)
        pending_kind = (
            SubmissionType.WOLF_ATTACK
            if wolves_split_pauses or any(m in wolves for m in missing)
            else SubmissionType.SEER_DIVINE
            if seer_seat in missing
            else SubmissionType.KNIGHT_GUARD
        )
        # Build the per-kind breakdown so the host UI can show every outstanding
        # action at once. Order matches role priority (wolf > seer > knight).
        per_kind: list[PendingSubmission] = []
        if wolf_missing_seats or wolf_unresolved_seats:
            per_kind.append(
                PendingSubmission(
                    submission_type=SubmissionType.WOLF_ATTACK,
                    missing_seats=wolf_missing_seats,
                    unresolved_seats=wolf_unresolved_seats,
                )
            )
        if seer_seat is not None and seer_seat in missing:
            per_kind.append(
                PendingSubmission(
                    submission_type=SubmissionType.SEER_DIVINE,
                    missing_seats=(seer_seat,),
                )
            )
        if knight_seat is not None and knight_seat in missing:
            per_kind.append(
                PendingSubmission(
                    submission_type=SubmissionType.KNIGHT_GUARD,
                    missing_seats=(knight_seat,),
                )
            )
        return Transition(
            next_phase=Phase.WAITING_HOST_DECISION,
            next_day=game.day_number,
            new_deadline_epoch=None,
            requires_host_decision=True,
            pending=PendingDecision(
                game_id=game.id,
                phase=Phase.NIGHT,
                day=game.day_number,
                required_submission=pending_kind,
                missing_seats=tuple(sorted(pending_missing)),
                submissions=tuple(per_kind),
                created_at=now_epoch,
            ),
        )

    private: list[LogEntry] = []

    # Step 1: Medium
    if medium_seat is not None:
        executed_today = next(
            (
                p
                for p in players
                if p.death_cause is DeathCause.EXECUTION and p.death_day == game.day_number
            ),
            None,
        )
        is_wolf = medium_detection(executed_today)
        if is_wolf is None:
            text = "本日の霊媒結果はありません(処刑なし)。"
        else:
            assert executed_today is not None
            name = _name(seats_by_no, executed_today.seat_no)
            text = (
                f"霊媒結果: {name} は 人狼 でした。"
                if is_wolf
                else f"霊媒結果: {name} は 人狼ではありませんでした。"
            )
        private.append(
            _private_log(
                game,
                audience_seat=medium_seat,
                kind="MEDIUM_RESULT",
                text=text,
                now_epoch=now_epoch,
            )
        )

    # Step 2: Seer
    if seer_seat is not None and seer_action is not None and seer_action.target_seat is not None:
        target = next((p for p in players if p.seat_no == seer_action.target_seat), None)
        if target is not None and target.role is not None:
            name = _name(seats_by_no, seer_action.target_seat)
            text = (
                f"占い結果: {name} は 人狼 です。"
                if is_detected_as_wolf(target.role)
                else f"占い結果: {name} は 人狼ではありません。"
            )
            private.append(
                _private_log(
                    game,
                    audience_seat=seer_seat,
                    kind="SEER_RESULT",
                    text=text,
                    now_epoch=now_epoch,
                )
            )

    # Step 3: Guard target (locked from knight_action; None if no submission or force-skip-missing)
    guard_target = knight_action.target_seat if knight_action is not None else None

    # Step 4/5/6: Attack / compare / resolve
    _attack: AttackResult = attack
    attack_target = _attack.target_seat
    # Defense-in-depth: submission-layer validation already rejects attacks on
    # dead seats, but if corrupt state slips through, refuse to overwrite
    # death_day — treat as a failed attack (no death).
    if attack_target is not None and not any(
        p.seat_no == attack_target and p.alive for p in players
    ):
        attack_target = None
    killed_seat: int | None = None
    if attack_target is not None and attack_target != guard_target:
        killed_seat = attack_target

    # Step 7: Morning announce
    if killed_seat is None:
        morning_text = "平和な朝です。昨晩の犠牲者はいません。"
    else:
        morning_text = f"朝になりました。犠牲者: {_name(seats_by_no, killed_seat)}"

    updates: list[PlayerUpdate] = []
    newly_dead: list[int] = []
    if killed_seat is not None:
        updates.append(
            PlayerUpdate(
                seat_no=killed_seat,
                alive=False,
                death_cause=DeathCause.ATTACK,
                death_day=game.day_number + 1,  # killed at dawn of next day
            )
        )
        newly_dead.append(killed_seat)

    # Step 9: Victory check on post-attack state
    new_players = [
        p.model_copy(update={"alive": False}) if p.seat_no in newly_dead else p for p in players
    ]
    v = check_victory(new_players)

    record_guard: tuple[int, int] | None = None
    if knight_seat is not None and guard_target is not None:
        record_guard = (knight_seat, guard_target)

    next_day = game.day_number + 1
    public_logs: tuple[LogEntry, ...] = (
        _public_log(
            game,
            kind="MORNING",
            text=morning_text,
            now_epoch=now_epoch,
            phase=Phase.DAY_DISCUSSION,
            actor_seat=killed_seat,
            day=next_day,
        ),
    )

    if v is not None:
        return Transition(
            next_phase=Phase.GAME_OVER,
            next_day=game.day_number,
            new_deadline_epoch=None,
            player_updates=tuple(updates),
            public_logs=(
                *public_logs,
                _victory_log(game, v, now_epoch),
                _role_reveal_log(game, new_players, seats_by_no, now_epoch),
            ),
            private_logs=tuple(private),
            victory=v,
            morning_text=morning_text,
            newly_dead_seats=tuple(newly_dead),
            record_guard=record_guard,
            clear_force_skip=True,
        )

    day_start = _public_log(
        game,
        kind="PHASE_CHANGE",
        text=(
            f"{next_day} 日目の議論を開始します。"
            f"制限時間は {day_discussion_duration(next_day)} 秒です。"
        ),
        now_epoch=now_epoch,
        phase=Phase.DAY_DISCUSSION,
        day=next_day,
    )
    return Transition(
        next_phase=Phase.DAY_DISCUSSION,
        next_day=next_day,
        new_deadline_epoch=now_epoch + day_discussion_duration(next_day),
        player_updates=tuple(updates),
        public_logs=(*public_logs, day_start),
        private_logs=tuple(private),
        morning_text=morning_text,
        newly_dead_seats=tuple(newly_dead),
        record_guard=record_guard,
        clear_force_skip=True,
    )


# ---------------------------------------------------------------- WAITING → resume
def plan_extend_deadline(game: Game, extra_seconds: int, now_epoch: int) -> Transition:
    """Host /wolf extend: re-open the paused phase with a new deadline.

    The caller must know the paused phase (from pending_decisions) and pass that as
    game.phase via a pre-swap — this function assumes game.phase already holds the target
    phase (not WAITING_HOST_DECISION). Returns a transition that just resets the deadline.
    """
    return Transition(
        next_phase=game.phase,
        next_day=game.day_number,
        new_deadline_epoch=now_epoch + extra_seconds,
    )


# ---------------------------------------------------------------- DAY_DISCUSSION wait
def plan_day_discussion_wait(game: Game, now_epoch: int) -> Transition:
    """Park in DAY_DISCUSSION with a short re-check deadline.

    Used when the discussion deadline has passed but at least one alive LLM seat
    hasn't completed both rounds. The next advance fires either when the LLM
    completion path wakes the engine or when this short grace deadline expires.
    The same-phase transition is intentional — `apply_transition`'s optimistic
    lock matches `expected_phase=DAY_DISCUSSION` even when next_phase equals it.
    """
    return Transition(
        next_phase=Phase.DAY_DISCUSSION,
        next_day=game.day_number,
        new_deadline_epoch=now_epoch + current_phase_durations().day_discussion_grace,
    )


# ---------------------------------------------------------------- DAY_RUNOFF_SPEECH → DAY_RUNOFF
def plan_runoff_speech_to_runoff(
    game: Game,
    seats_by_no: Mapping[int, Seat],
    tied_candidates: Sequence[int],
    now_epoch: int,
) -> Transition:
    """DAY_RUNOFF_SPEECH → DAY_RUNOFF after candidate LLMs have spoken.

    `tied_candidates` is recomputed by the caller from round 0 votes — never
    stored separately, since `compute_vote_result` is pure and idempotent.
    """
    candidates_text = "、".join(_name(seats_by_no, s) for s in tied_candidates)
    pub = _public_log(
        game,
        kind="PHASE_CHANGE",
        text=f"決選投票を開始します。候補: {candidates_text}",
        now_epoch=now_epoch,
        phase=Phase.DAY_RUNOFF,
    )
    return Transition(
        next_phase=Phase.DAY_RUNOFF,
        next_day=game.day_number,
        new_deadline_epoch=now_epoch + current_phase_durations().runoff,
        public_logs=(pub,),
    )


def plan_runoff_speech_wait(game: Game, now_epoch: int) -> Transition:
    """Park in DAY_RUNOFF_SPEECH with a short grace deadline.

    Mirrors `plan_day_discussion_wait` for the runoff-speech intermediate phase.
    Triggers when the safety-net deadline expires before all candidate LLMs have
    completed `runoff_speech_done`; the LLM dispatcher's `finally` block also
    wakes the engine when each seat finishes.
    """
    return Transition(
        next_phase=Phase.DAY_RUNOFF_SPEECH,
        next_day=game.day_number,
        new_deadline_epoch=now_epoch + current_phase_durations().runoff_speech_grace,
    )


# Names listed below that are NOT bound at module level
# (DAY_DISCUSSION_GRACE / NIGHT_DURATION / RUNOFF_DURATION /
# RUNOFF_SPEECH_GRACE / VOTE_DURATION) are exported dynamically through
# the module-level ``__getattr__`` defined above (PEP 562). They are
# intentionally part of the public API for backwards compatibility with
# code that imports the historical constants. Ruff cannot see the
# dynamic binding, hence the ``noqa: F822`` markers.
__all__ = [
    "DAY_DISCUSSION_GRACE",  # noqa: F822 — see PEP 562 __getattr__ above
    "NIGHT_DURATION",  # noqa: F822
    "RUNOFF_DURATION",  # noqa: F822
    "RUNOFF_SPEECH_DEADLINE",
    "RUNOFF_SPEECH_GRACE",  # noqa: F822
    "VOTE_DURATION",  # noqa: F822
    "plan_day_discussion_to_vote",
    "plan_day_discussion_wait",
    "plan_day_runoff_resolve",
    "plan_day_vote_resolve",
    "plan_extend_deadline",
    "plan_night0",
    "plan_night_resolve",
    "plan_runoff_speech_to_runoff",
    "plan_runoff_speech_wait",
    "plan_setup",
]
