"""discord.ui.View subclasses for DM-based secret submissions.

Each view holds a discord.ui.Select with the seat's legal candidates. When the user
picks one, `on_submit` is called with (game_id, actor_seat, target_seat, kind_or_round)
and the view disables itself.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence

import discord

from wolfbot.domain.enums import SubmissionType
from wolfbot.domain.models import Seat

log = logging.getLogger(__name__)


class VoteView(discord.ui.View):
    """DM vote UI. Round 0 = main vote, 1 = runoff."""

    def __init__(
        self,
        game_id: str,
        voter_seat: int,
        candidates: Sequence[Seat],
        round_: int,
        on_submit: Callable[[str, int, int, int], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self.voter_seat = voter_seat
        self.round_ = round_
        self._on_submit = on_submit

        options = [
            discord.SelectOption(label=s.display_name, value=str(s.seat_no))
            for s in candidates
            if s.seat_no != voter_seat
        ][:25]
        placeholder = "投票先を選んでください" if round_ == 0 else "決選投票先を選んでください"
        self.select_target: discord.ui.Select[VoteView] = discord.ui.Select(
            placeholder=placeholder, min_values=1, max_values=1, options=options
        )
        self.select_target.callback = self._on_pick  # type: ignore[method-assign]
        self.add_item(self.select_target)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        target_seat = int(self.select_target.values[0])
        try:
            await self._on_submit(
                self.game_id, self.voter_seat, target_seat, self.round_
            )
        except Exception:
            log.exception("vote submission callback failed")
            await interaction.response.send_message(
                "投票処理中にエラーが発生しました。", ephemeral=True
            )
            return
        self.select_target.disabled = True
        try:
            await interaction.response.edit_message(
                content="投票を受け付けました。", view=self
            )
        except discord.InteractionResponded:
            await interaction.followup.send("投票を受け付けました。", ephemeral=True)
        self.stop()


class NightActionView(discord.ui.View):
    """DM night-action UI. Kind determines the prompt and expected callback kind."""

    def __init__(
        self,
        game_id: str,
        actor_seat: int,
        kind: SubmissionType,
        candidates: Sequence[Seat],
        on_submit: Callable[[str, int, SubmissionType, int], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self.actor_seat = actor_seat
        self.kind = kind
        self._on_submit = on_submit

        placeholders = {
            SubmissionType.WOLF_ATTACK: "襲撃対象を選んでください",
            SubmissionType.SEER_DIVINE: "占う相手を選んでください",
            SubmissionType.KNIGHT_GUARD: "護衛する相手を選んでください",
        }
        placeholder = placeholders.get(kind, "対象を選んでください")
        options = [
            discord.SelectOption(label=s.display_name, value=str(s.seat_no))
            for s in candidates
        ][:25]
        self.select_target: discord.ui.Select[NightActionView] = discord.ui.Select(
            placeholder=placeholder, min_values=1, max_values=1, options=options
        )
        self.select_target.callback = self._on_pick  # type: ignore[method-assign]
        self.add_item(self.select_target)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        target_seat = int(self.select_target.values[0])
        try:
            await self._on_submit(self.game_id, self.actor_seat, self.kind, target_seat)
        except Exception:
            log.exception("night action submission callback failed")
            await interaction.response.send_message(
                "行動の提出中にエラーが発生しました。", ephemeral=True
            )
            return
        self.select_target.disabled = True
        try:
            await interaction.response.edit_message(
                content="行動を受け付けました。", view=self
            )
        except discord.InteractionResponded:
            await interaction.followup.send("行動を受け付けました。", ephemeral=True)
        self.stop()
