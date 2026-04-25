"""Enums that describe roles, phases, factions, and submission kinds.

Keep this module side-effect free — pure layer.
"""

from __future__ import annotations

from enum import StrEnum


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
