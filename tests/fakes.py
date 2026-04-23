"""Fake DiscordAdapter and LLMAdapter for in-process tests.

These capture every call for later assertions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from wolfbot.domain.models import Game, PendingDecision, Player, Seat


@dataclass
class Recorded:
    name: str
    kwargs: dict[str, Any]


@dataclass
class FakeDiscordAdapter:
    calls: list[Recorded] = field(default_factory=list)
    fail_on: set[str] = field(default_factory=set)  # set of call names to raise on

    def reset(self) -> None:
        self.calls.clear()

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append(Recorded(name=name, kwargs=kwargs))
        if name in self.fail_on:
            raise RuntimeError(f"fake discord failure on {name}")

    async def apply_permissions(
        self, game: Game, seats: Sequence[Seat], players: Sequence[Player]
    ) -> None:
        self._record("apply_permissions", game_id=game.id, phase=game.phase)

    async def kill_permissions(
        self, game: Game, seats: Sequence[Seat], seat_no: int, was_wolf: bool
    ) -> None:
        self._record("kill_permissions", game_id=game.id, seat=seat_no, was_wolf=was_wolf)

    async def reconcile(
        self, game: Game, seats: Sequence[Seat], players: Sequence[Player] | None = None
    ) -> None:
        self._record("reconcile", game_id=game.id)

    async def on_game_end(self, game: Game, seats: Sequence[Seat]) -> None:
        self._record("on_game_end", game_id=game.id)

    async def post_public(self, game: Game, text: str, kind: str) -> None:
        self._record("post_public", game_id=game.id, text=text, kind=kind)

    async def post_morning(self, game: Game, text: str) -> None:
        self._record("post_morning", game_id=game.id, text=text)

    async def send_private(self, game: Game, audience_seat: int, text: str, kind: str) -> None:
        self._record(
            "send_private",
            game_id=game.id,
            audience=audience_seat,
            text=text,
            kind=kind,
        )

    async def send_vote_dms(
        self,
        game: Game,
        voters: Sequence[Player],
        candidates: Sequence[Seat],
        round_: int,
    ) -> None:
        self._record(
            "send_vote_dms",
            game_id=game.id,
            voters=[p.seat_no for p in voters],
            candidates=[s.seat_no for s in candidates],
            round_=round_,
        )

    async def send_night_action_dms(
        self, game: Game, players: Sequence[Player], seats: Sequence[Seat]
    ) -> None:
        self._record(
            "send_night_action_dms",
            game_id=game.id,
            players=[p.seat_no for p in players],
        )

    async def announce_waiting(
        self,
        game: Game,
        pending: PendingDecision,
        seats: Sequence[Seat],
    ) -> None:
        self._record(
            "announce_waiting",
            game_id=game.id,
            phase=pending.phase,
            missing=pending.missing_seats,
        )

    async def announce_recovery(self, game: Game, pending: PendingDecision | None) -> None:
        self._record(
            "announce_recovery",
            game_id=game.id,
            phase=game.phase,
            pending_phase=pending.phase if pending else None,
        )


@dataclass
class FakeLLMAdapter:
    """Default: do nothing (tests drive LLM submissions manually when needed)."""

    calls: list[Recorded] = field(default_factory=list)

    async def submit_llm_night_actions(
        self, game: Game, players: Sequence[Player], seats: Sequence[Seat]
    ) -> None:
        self.calls.append(
            Recorded(
                "submit_llm_night_actions",
                {"game_id": game.id, "players": [p.seat_no for p in players]},
            )
        )

    async def submit_llm_votes(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        candidates: Sequence[int] | None,
        round_: int,
    ) -> None:
        self.calls.append(
            Recorded(
                "submit_llm_votes",
                {
                    "game_id": game.id,
                    "voters": [p.seat_no for p in players],
                    "candidates": list(candidates) if candidates else None,
                    "round_": round_,
                },
            )
        )

    async def submit_llm_daystart_speeches(
        self, game: Game, players: Sequence[Player], seats: Sequence[Seat]
    ) -> None:
        self.calls.append(
            Recorded(
                "submit_llm_daystart_speeches",
                {"game_id": game.id, "players": [p.seat_no for p in players]},
            )
        )


@dataclass
class FakeClock:
    now: int = 0

    def tick(self, seconds: int) -> None:
        self.now += seconds

    def __call__(self) -> int:
        return self.now
