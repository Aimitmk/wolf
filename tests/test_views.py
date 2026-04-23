"""Tests for the DM VoteView / NightActionView stale-submission feedback.

These ensure that when GameService.submit_vote/submit_night_action returns a
SubmitResult other than ACCEPTED (e.g. stale phase, dead voter), the view tells
the user explicitly instead of silently claiming success.
"""

from __future__ import annotations

from typing import Any, cast

from wolfbot.domain.enums import SubmissionType, SubmitResult
from wolfbot.domain.models import Seat
from wolfbot.ui.views import NightActionView, VoteView


class FakeResponse:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, bool]] = []
        self.edited_messages: list[str] = []

    async def send_message(self, text: str, ephemeral: bool = False) -> None:
        self.sent_messages.append((text, ephemeral))

    async def edit_message(self, content: str | None = None, view: Any = None) -> None:
        self.edited_messages.append(content or "")


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    async def send(self, text: str, ephemeral: bool = False) -> None:
        self.sent.append((text, ephemeral))


class FakeInteraction:
    def __init__(self) -> None:
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def _seat(seat_no: int) -> Seat:
    return Seat(
        seat_no=seat_no,
        display_name=f"P{seat_no}",
        discord_user_id=f"u{seat_no}",
        is_llm=False,
        persona_key=None,
    )


async def test_vote_view_on_accepted_shows_success() -> None:
    async def on_submit(game_id: str, voter: int, target: int, round_: int) -> SubmitResult:
        return SubmitResult.ACCEPTED

    view = VoteView(
        game_id="g",
        voter_seat=1,
        candidates=[_seat(2), _seat(3)],
        round_=0,
        on_submit=on_submit,
    )
    view.select_target._values = ["2"]  # type: ignore[attr-defined]

    interaction = FakeInteraction()
    await view._on_pick(cast(Any, interaction))

    assert interaction.response.edited_messages == ["投票を受け付けました。"]
    assert interaction.response.sent_messages == []
    assert view.select_target.disabled is True


async def test_vote_view_on_stale_phase_shows_reject() -> None:
    async def on_submit(game_id: str, voter: int, target: int, round_: int) -> SubmitResult:
        return SubmitResult.STALE_PHASE

    view = VoteView(
        game_id="g",
        voter_seat=1,
        candidates=[_seat(2), _seat(3)],
        round_=0,
        on_submit=on_submit,
    )
    view.select_target._values = ["2"]  # type: ignore[attr-defined]

    interaction = FakeInteraction()
    await view._on_pick(cast(Any, interaction))

    assert interaction.response.sent_messages, "rejection must be surfaced to the user, not hidden"
    text, ephemeral = interaction.response.sent_messages[0]
    assert "投票フェイズ" in text
    assert ephemeral is True
    assert interaction.response.edited_messages == []
    # Select stays enabled so user can potentially re-click if a new DM arrives.
    assert view.select_target.disabled is False


async def test_vote_view_on_target_dead_says_target_dead() -> None:
    async def on_submit(game_id: str, voter: int, target: int, round_: int) -> SubmitResult:
        return SubmitResult.TARGET_DEAD

    view = VoteView(
        game_id="g",
        voter_seat=1,
        candidates=[_seat(2), _seat(3)],
        round_=0,
        on_submit=on_submit,
    )
    view.select_target._values = ["3"]  # type: ignore[attr-defined]

    interaction = FakeInteraction()
    await view._on_pick(cast(Any, interaction))

    assert any("亡くな" in text for text, _ in interaction.response.sent_messages)


async def test_night_view_on_accepted_shows_success() -> None:
    async def on_submit(
        game_id: str, actor: int, kind: SubmissionType, target: int
    ) -> SubmitResult:
        return SubmitResult.ACCEPTED

    view = NightActionView(
        game_id="g",
        actor_seat=2,
        kind=SubmissionType.SEER_DIVINE,
        candidates=[_seat(3), _seat(4)],
        on_submit=on_submit,
    )
    view.select_target._values = ["3"]  # type: ignore[attr-defined]

    interaction = FakeInteraction()
    await view._on_pick(cast(Any, interaction))

    assert interaction.response.edited_messages == ["行動を受け付けました。"]
    assert view.select_target.disabled is True


async def test_night_view_on_role_mismatch_shows_reject() -> None:
    async def on_submit(
        game_id: str, actor: int, kind: SubmissionType, target: int
    ) -> SubmitResult:
        return SubmitResult.ROLE_MISMATCH

    view = NightActionView(
        game_id="g",
        actor_seat=2,
        kind=SubmissionType.SEER_DIVINE,
        candidates=[_seat(3), _seat(4)],
        on_submit=on_submit,
    )
    view.select_target._values = ["3"]  # type: ignore[attr-defined]

    interaction = FakeInteraction()
    await view._on_pick(cast(Any, interaction))

    assert interaction.response.sent_messages, (
        "role mismatch must be surfaced, not silently dropped"
    )
    text, _ = interaction.response.sent_messages[0]
    assert "役職" in text
    assert interaction.response.edited_messages == []
