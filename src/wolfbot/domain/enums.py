"""Enums that describe roles, phases, factions, and submission kinds.

Keep this module side-effect free — pure layer.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal


class Role(StrEnum):
    WEREWOLF = "WEREWOLF"
    MADMAN = "MADMAN"
    SEER = "SEER"
    MEDIUM = "MEDIUM"
    KNIGHT = "KNIGHT"
    VILLAGER = "VILLAGER"


ROLE_JA: dict[Role, str] = {
    Role.WEREWOLF: "人狼",
    Role.MADMAN: "狂人",
    Role.SEER: "占い師",
    Role.MEDIUM: "霊媒師",
    Role.KNIGHT: "騎士",
    Role.VILLAGER: "村人",
}


class Phase(StrEnum):
    LOBBY = "LOBBY"
    SETUP = "SETUP"
    NIGHT_0 = "NIGHT_0"
    DAY_DISCUSSION = "DAY_DISCUSSION"
    DAY_VOTE = "DAY_VOTE"
    DAY_RUNOFF_SPEECH = "DAY_RUNOFF_SPEECH"
    DAY_RUNOFF = "DAY_RUNOFF"
    NIGHT = "NIGHT"
    WAITING_HOST_DECISION = "WAITING_HOST_DECISION"
    GAME_OVER = "GAME_OVER"


class Faction(StrEnum):
    VILLAGE = "VILLAGE"
    WEREWOLVES = "WEREWOLVES"


FACTION_JA: dict[Faction, str] = {
    Faction.VILLAGE: "村人陣営",
    Faction.WEREWOLVES: "人狼陣営",
}


class Intent(StrEnum):
    SPEAK = "speak"
    VOTE = "vote"
    NIGHT_ACTION = "night_action"
    SKIP = "skip"


class DeathCause(StrEnum):
    EXECUTION = "EXECUTION"
    ATTACK = "ATTACK"


class SubmissionType(StrEnum):
    VOTE = "VOTE"
    RUNOFF_VOTE = "RUNOFF_VOTE"
    WOLF_ATTACK = "WOLF_ATTACK"
    SEER_DIVINE = "SEER_DIVINE"
    KNIGHT_GUARD = "KNIGHT_GUARD"


class SubmitResult(StrEnum):
    """Outcome of GameService.submit_vote / submit_night_action.

    UI layers branch on this to tell the user whether a click was accepted
    vs silently dropped because the DM is stale/invalid.
    """

    ACCEPTED = "ACCEPTED"
    STALE_PHASE = "STALE_PHASE"
    GAME_NOT_FOUND = "GAME_NOT_FOUND"
    VOTER_DEAD = "VOTER_DEAD"
    TARGET_DEAD = "TARGET_DEAD"
    SELF_VOTE = "SELF_VOTE"
    ACTOR_DEAD = "ACTOR_DEAD"
    ROLE_MISMATCH = "ROLE_MISMATCH"
    ILLEGAL_TARGET = "ILLEGAL_TARGET"


FACTION_OF_ROLE: dict[Role, Faction] = {
    Role.WEREWOLF: Faction.WEREWOLVES,
    Role.MADMAN: Faction.WEREWOLVES,
    Role.SEER: Faction.VILLAGE,
    Role.MEDIUM: Faction.VILLAGE,
    Role.KNIGHT: Faction.VILLAGE,
    Role.VILLAGER: Faction.VILLAGE,
}


ROLE_DISTRIBUTION: dict[Role, int] = {
    Role.WEREWOLF: 2,
    Role.MADMAN: 1,
    Role.SEER: 1,
    Role.MEDIUM: 1,
    Role.KNIGHT: 1,
    Role.VILLAGER: 3,
}


VILLAGE_SIZE = 9


# Roles that may openly claim themselves ("カミングアウト" / CO) during day
# discussion. Wolves and madmen routinely fake these, but they cannot CO
# their own true role (no one openly claims wolf/madman); villagers have
# no role power so a villager-CO is meaningless. The lowercased form is
# the wire/storage shape used by ``speech_events.co_declaration``, the
# voice/text analyzer prompts, and downstream Literal validators.
CO_CLAIMABLE_ROLES: tuple[Role, ...] = (Role.SEER, Role.MEDIUM, Role.KNIGHT)


def role_to_co_claim(role: Role) -> str:
    """Wire form of a CO-claimable role (``Role.SEER`` → ``"seer"``)."""
    return role.value.lower()


CO_CLAIM_VALUES: tuple[str, ...] = tuple(
    role_to_co_claim(r) for r in CO_CLAIMABLE_ROLES
)


# Role-callout values are a SUPERSET of CO_CLAIM_VALUES — specifically
# they include the synthetic ``"info_request"`` token that the speech
# analyzers emit when a speaker generically asks for opinions / role
# holders without naming a specific role. Examples (from real game logs):
#   - "誰か怪しい人いる?"
#   - "みんな意見を聞かせて"
#   - "気になる人を挙げて"
#   - "誰か役職持ち、出てきて"
# The arbiter's role-callout priority pool treats ``"info_request"`` as
# "all info roles + all wolf-side" so the village can extract early
# information from a generic prompt, not only role-specific prompts.
ROLE_CALLOUT_VALUES: tuple[str, ...] = (*CO_CLAIM_VALUES, "info_request")


# Type alias for the wire/storage form. Cannot be derived from
# ``CO_CLAIM_VALUES`` because :class:`Literal` requires static values
# resolvable by the type-checker — ``Literal[*tuple]`` is not legal
# Python. Adding a CO-claimable role therefore requires updating BOTH
# :data:`CO_CLAIMABLE_ROLES` and this Literal in lockstep; the
# consistency test in ``tests/test_domain_co_claim.py`` asserts the two
# stay aligned so a mismatch is caught at CI time rather than at
# runtime when a mis-typed seat starts dropping CO declarations
# silently.
#
# Named ``CoDeclaration`` rather than ``CoClaim`` because
# :class:`wolfbot.domain.discussion.CoClaim` is already a Pydantic
# model representing a CO event in public-state rebuild — the two
# concepts are unrelated and the name collision would be confusing.
CoDeclaration = Literal["seer", "medium", "knight"]


def format_co_claim_options(
    *, separator: str = "/", quote: str = '"'
) -> str:
    """Render :data:`CO_CLAIM_VALUES` as a prompt-friendly enumeration.

    Default form ``"seer"/"medium"/"knight"`` matches the existing
    Japanese analyzer prompts. Pass ``quote=""`` for a bare list
    (``seer/medium/knight``).
    """
    return separator.join(f"{quote}{v}{quote}" for v in CO_CLAIM_VALUES)
