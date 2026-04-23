"""Domain models (Pydantic v2).

- Immutable (frozen): Seat, LogEntry, PendingDecision, PlayerUpdate, Transition, VoteOutcome.
- Mutable: Player, Game, NightAction, Vote — used as live state the services mutate.

Kept free of I/O. No aiosqlite or discord imports.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from wolfbot.domain.enums import DeathCause, Faction, Phase, Role, SubmissionType


class Seat(BaseModel):
    """Immutable seating assignment decided at /wolf start."""

    model_config = ConfigDict(frozen=True)

    seat_no: int = Field(ge=1, le=9)
    display_name: str
    discord_user_id: str | None  # None when is_llm
    is_llm: bool
    persona_key: str | None  # Gnosia persona_key for LLM seats


class Player(BaseModel):
    """Mutable per-game state for a Seat."""

    seat_no: int = Field(ge=1, le=9)
    role: Role | None = None  # assigned at SETUP
    alive: bool = True
    death_cause: DeathCause | None = None
    death_day: int | None = None
    dm_channel_id: str | None = None


class Game(BaseModel):
    """Top-level game state; one row in `games` table."""

    id: str  # uuid4
    guild_id: str
    host_user_id: str
    phase: Phase = Phase.LOBBY
    day_number: int = 0
    deadline_epoch: int | None = None
    main_text_channel_id: str
    main_vc_channel_id: str
    heaven_channel_id: str | None = None
    wolves_channel_id: str | None = None
    created_at: int
    ended_at: int | None = None
    force_skip_pending: bool = False


class NightAction(BaseModel):
    game_id: str
    day: int
    actor_seat: int
    kind: SubmissionType
    target_seat: int | None
    submitted_at: int


class Vote(BaseModel):
    game_id: str
    day: int
    round: int = Field(ge=0, le=1)
    voter_seat: int
    target_seat: int | None
    submitted_at: int


class LogEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    game_id: str
    day: int
    phase: Phase
    kind: str
    actor_seat: int | None
    visibility: Literal["PUBLIC", "PRIVATE"]
    audience_seat: int | None = None  # for PRIVATE
    text: str
    payload_json: str | None = None
    created_at: int


class PendingSubmission(BaseModel):
    """A single outstanding submission kind and the seats that owe it.

    NIGHT typically carries up to three of these (wolf attack, seer divine, knight
    guard). DAY_VOTE / DAY_RUNOFF carry exactly one.
    """

    model_config = ConfigDict(frozen=True)

    submission_type: SubmissionType
    missing_seats: tuple[int, ...]


class PendingDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    game_id: str
    phase: Phase
    day: int
    # Primary (legacy) view: the single "most pressing" submission. For NIGHT with
    # multiple missing kinds, this is the first entry of `submissions`. Preserved
    # for existing DB rows and status messages that only know one type.
    required_submission: SubmissionType
    missing_seats: tuple[int, ...]
    # Full breakdown. Empty tuple is valid only for pre-Fix-3 rows loaded from DB;
    # call sites that construct a fresh PendingDecision should populate this.
    submissions: tuple[PendingSubmission, ...] = ()
    created_at: int

    def effective_submissions(self) -> tuple[PendingSubmission, ...]:
        """Return `submissions` if present, else synthesize a single entry from
        the legacy primary fields. Use this for display to cover old DB rows."""
        if self.submissions:
            return self.submissions
        return (
            PendingSubmission(
                submission_type=self.required_submission,
                missing_seats=self.missing_seats,
            ),
        )


class PlayerUpdate(BaseModel):
    """A diff applied to a Player inside a Transition."""

    model_config = ConfigDict(frozen=True)

    seat_no: int
    alive: bool | None = None
    death_cause: DeathCause | None = None
    death_day: int | None = None
    role: Role | None = None  # only used in plan_setup


class VoteOutcome(BaseModel):
    """Result of compute_vote_result."""

    model_config = ConfigDict(frozen=True)

    executed: int | None = None  # seat_no of executed player, if decided
    tied: tuple[int, ...] = ()  # seat_nos tied at max (0-length when settled)


class Transition(BaseModel):
    """Command package produced by pure state_machine.plan_* functions.

    The game_service applies this in order: permissions -> announce -> DM -> commit.
    If requires_host_decision is True, the engine parks until /wolf extend or /wolf force-skip.
    """

    model_config = ConfigDict(frozen=True)

    next_phase: Phase
    next_day: int
    new_deadline_epoch: int | None = None
    player_updates: tuple[PlayerUpdate, ...] = ()
    public_logs: tuple[LogEntry, ...] = ()
    private_logs: tuple[LogEntry, ...] = ()
    requires_host_decision: bool = False
    pending: PendingDecision | None = None
    victory: Faction | None = None
    morning_text: str | None = None
    clear_force_skip: bool = False
    # Instructions for downstream services:
    # - seat_nos whose permissions need updating (killed this transition)
    newly_dead_seats: tuple[int, ...] = ()
    # - if True, the next night's previous_guard should be set to (knight_seat, target_seat)
    record_guard: tuple[int, int] | None = None


class AttackResult(BaseModel):
    """Return of rules.resolve_wolf_attack."""

    model_config = ConfigDict(frozen=True)

    target_seat: int | None = None  # None = no attack
    split: bool = False  # True if wolves disagreed (at least 2 alive + picks differed)
    missing: tuple[int, ...] = ()  # wolves that didn't submit
