"""`/wolf settings` interactive UI for runtime-mutable phase durations.

Permission model: only the host of the currently-active game can open the
view, and `interaction_check` re-validates on every nested interaction so
a passed-around ephemeral message still rejects non-host clicks.

Persistence model: process memory only — `set_phase_durations` swaps the
singleton in `wolfbot.domain.durations`. A Master restart resets to env-
derived defaults. This matches the user's choice of "no DB" for v1; if
persistence is added later, the View hooks stay the same and only the
write path needs to also persist.

Components:
- ``PhaseDurationsView``: top-level view with a per-field Select, a
  global-factor Button, and a "reset to defaults" Button.
- ``_DurationEditModal``: 1-field modal opened by the Select for fine-
  grained edits (current value pre-filled).
- ``_FactorModal``: 1-field modal for the with_factor bulk knob.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import discord

from wolfbot.domain.durations import (
    PhaseDurations,
    current_phase_durations,
    reset_phase_durations_to_defaults,
    set_phase_durations,
)

log = logging.getLogger(__name__)


_DURATION_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("vote", "投票"),
    ("runoff", "決選投票"),
    ("night", "夜"),
    ("day_discussion_grace", "議論猶予"),
    ("runoff_speech_grace", "決選演説猶予"),
    ("discussion_day1", "議論 (1日目)"),
    ("discussion_day2", "議論 (2日目)"),
    ("discussion_day3plus", "議論 (3日目以降)"),
)


def _format_durations_embed(d: PhaseDurations) -> discord.Embed:
    embed = discord.Embed(
        title="⚙️ Phase Durations",
        description=(
            "プロセスメモリ上で設定が反映されます (Master 再起動でデフォルトに戻ります)。\n"
            "現在の値を選択肢から編集するか、ボタンで一括変更できます。"
        ),
        color=discord.Color.blurple(),
    )
    for field_name, label in _DURATION_FIELD_LABELS:
        embed.add_field(
            name=f"{label} ({field_name})",
            value=f"`{getattr(d, field_name)}` 秒",
            inline=True,
        )
    return embed


class _DurationEditModal(discord.ui.Modal):
    """Single-field modal that updates one PhaseDurations attribute."""

    def __init__(
        self,
        *,
        field_name: str,
        label: str,
        current_value: int,
        parent: PhaseDurationsView,
    ) -> None:
        super().__init__(title=f"{label} を変更", timeout=180)
        self._field_name = field_name
        self._parent = parent
        self.value_input: discord.ui.TextInput[_DurationEditModal] = discord.ui.TextInput(
            label=f"{label} (秒、整数 ≥1)",
            default=str(current_value),
            required=True,
            max_length=8,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.value_input.value.strip()
        try:
            value = int(raw)
        except ValueError:
            await interaction.response.send_message(
                f"整数で入力してください (`{raw}` は不正)。", ephemeral=True
            )
            return
        if value < 1:
            await interaction.response.send_message(
                "1 秒以上の値を指定してください。", ephemeral=True
            )
            return
        new_durations = replace(current_phase_durations(), **{self._field_name: value})
        set_phase_durations(new_durations)
        log.info(
            "phase_duration_updated field=%s value=%d by_user=%s",
            self._field_name,
            value,
            interaction.user.id,
        )
        await self._parent.refresh(interaction, new_durations)


class _FactorModal(discord.ui.Modal):
    """Modal for the bulk `with_factor` knob."""

    def __init__(self, *, parent: PhaseDurationsView) -> None:
        super().__init__(title="全フェイズを一括スケール", timeout=180)
        self._parent = parent
        self.factor_input: discord.ui.TextInput[_FactorModal] = discord.ui.TextInput(
            label="スケール係数 (例 0.5 で半分、2.0 で倍)",
            default="1.0",
            required=True,
            max_length=8,
        )
        self.add_item(self.factor_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.factor_input.value.strip()
        try:
            factor = float(raw)
        except ValueError:
            await interaction.response.send_message(
                f"数値で入力してください (`{raw}` は不正)。", ephemeral=True
            )
            return
        if factor <= 0:
            await interaction.response.send_message(
                "0 より大きい値を指定してください。", ephemeral=True
            )
            return
        new_durations = current_phase_durations().with_factor(factor)
        set_phase_durations(new_durations)
        log.info(
            "phase_durations_scaled factor=%s by_user=%s",
            factor,
            interaction.user.id,
        )
        await self._parent.refresh(interaction, new_durations)


class _DurationFieldSelect(discord.ui.Select["PhaseDurationsView"]):
    """Select dropdown listing each duration field with its current value."""

    def __init__(self, current: PhaseDurations) -> None:
        options = [
            discord.SelectOption(
                label=f"{label}",
                value=field_name,
                description=f"現在 {getattr(current, field_name)} 秒",
            )
            for field_name, label in _DURATION_FIELD_LABELS
        ]
        super().__init__(
            placeholder="編集する項目を選択…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        field_name = self.values[0]
        label = next(
            (lbl for fn, lbl in _DURATION_FIELD_LABELS if fn == field_name),
            field_name,
        )
        modal = _DurationEditModal(
            field_name=field_name,
            label=label,
            current_value=getattr(current_phase_durations(), field_name),
            parent=self.view,
        )
        await interaction.response.send_modal(modal)


class PhaseDurationsView(discord.ui.View):
    """Top-level View backing `/wolf settings`.

    Restricted to ``host_user_id`` — every interaction is gated through
    ``interaction_check`` so the ephemeral message can't be exploited
    even if its token leaks. The original slash command handler is also
    expected to do the same check before posting the View; this is the
    second line of defense.
    """

    def __init__(self, *, host_user_id: str, timeout: float = 600.0) -> None:
        super().__init__(timeout=timeout)
        self._host_user_id = host_user_id
        self._select = _DurationFieldSelect(current_phase_durations())
        self.add_item(self._select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self._host_user_id:
            await interaction.response.send_message(
                "この設定パネルはホスト専用です。", ephemeral=True
            )
            return False
        return True

    async def refresh(
        self, interaction: discord.Interaction, new_durations: PhaseDurations
    ) -> None:
        """Re-render the embed + select after a value changed."""
        # Rebuild the select so its option descriptions reflect the new
        # current values (Discord won't re-query a Select's options).
        self.remove_item(self._select)
        self._select = _DurationFieldSelect(new_durations)
        self.add_item(self._select)
        embed = _format_durations_embed(new_durations)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label="一括スケール…",
        style=discord.ButtonStyle.secondary,
        custom_id="wolfbot:settings:factor",
    )
    async def factor_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[PhaseDurationsView],
    ) -> None:
        await interaction.response.send_modal(_FactorModal(parent=self))

    @discord.ui.button(
        label="デフォルトに戻す",
        style=discord.ButtonStyle.danger,
        custom_id="wolfbot:settings:reset",
    )
    async def reset_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[PhaseDurationsView],
    ) -> None:
        reset_phase_durations_to_defaults()
        log.info(
            "phase_durations_reset_to_defaults by_user=%s", interaction.user.id
        )
        await self.refresh(interaction, current_phase_durations())


def render_initial_message(
    *, host_user_id: str
) -> tuple[discord.Embed, PhaseDurationsView]:
    """Build the embed + view used by the `/wolf settings` slash command."""
    view = PhaseDurationsView(host_user_id=host_user_id)
    embed = _format_durations_embed(current_phase_durations())
    return embed, view


__all__ = [
    "PhaseDurationsView",
    "render_initial_message",
]
