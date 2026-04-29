"""`/wolf settings` interactive view — unit coverage.

The view itself is a discord.ui composition; we exercise the embed + view
construction, the duration mutation logic, and the interaction guard. We
mock just enough of `discord.Interaction` to test the logic without
spinning up the bot.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from wolfbot.domain.durations import (
    PhaseDurations,
    current_phase_durations,
    reset_phase_durations_to_defaults,
    set_phase_durations,
)
from wolfbot.ui.settings_view import (
    _DURATION_FIELD_LABELS,
    PhaseDurationsView,
    render_initial_message,
)


@pytest.fixture(autouse=True)
def _reset_durations() -> None:
    """Each test starts from clean defaults — the singleton is process-
    global, so leakage across tests would otherwise compound."""
    reset_phase_durations_to_defaults()


def test_render_initial_message_lists_all_fields() -> None:
    embed, view = render_initial_message(host_user_id="host1")
    assert isinstance(view, PhaseDurationsView)
    field_names = {f.name for f in embed.fields}
    for fn, label in _DURATION_FIELD_LABELS:
        # Each line is "label (field_name)" — both substrings must appear.
        assert any(label in name and fn in name for name in field_names), fn


def test_render_initial_message_reflects_current_singleton() -> None:
    set_phase_durations(replace(current_phase_durations(), vote=42))
    embed, _view = render_initial_message(host_user_id="host1")
    vote_field = next(
        f for f in embed.fields if "vote" in f.name
    )
    assert "42" in vote_field.value


async def test_interaction_check_rejects_non_host() -> None:
    view = PhaseDurationsView(host_user_id="host1")
    interaction = MagicMock()
    interaction.user.id = 9999  # not the host
    interaction.response.send_message = AsyncMock()
    ok = await view.interaction_check(interaction)
    assert ok is False
    interaction.response.send_message.assert_called_once()
    _args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


async def test_interaction_check_accepts_host() -> None:
    view = PhaseDurationsView(host_user_id="host1")
    interaction = MagicMock()
    interaction.user.id = "host1"  # may arrive as int or str depending on call site
    interaction.response.send_message = AsyncMock()
    ok = await view.interaction_check(interaction)
    assert ok is True
    interaction.response.send_message.assert_not_called()


async def test_refresh_updates_singleton_via_modal_submit() -> None:
    """Submitting an integer through the duration edit modal must swap
    the singleton and re-render the embed."""
    from wolfbot.ui.settings_view import _DurationEditModal

    view = PhaseDurationsView(host_user_id="host1")
    modal = _DurationEditModal(
        field_name="vote",
        label="投票",
        current_value=current_phase_durations().vote,
        parent=view,
    )
    modal.value_input = MagicMock()
    modal.value_input.value = "45"
    interaction = MagicMock()
    interaction.user.id = "host1"
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    await modal.on_submit(interaction)
    assert current_phase_durations().vote == 45
    interaction.response.edit_message.assert_called_once()


async def test_refresh_rejects_non_integer() -> None:
    from wolfbot.ui.settings_view import _DurationEditModal

    view = PhaseDurationsView(host_user_id="host1")
    modal = _DurationEditModal(
        field_name="vote",
        label="投票",
        current_value=60,
        parent=view,
    )
    modal.value_input = MagicMock()
    modal.value_input.value = "not-a-number"
    interaction = MagicMock()
    interaction.user.id = "host1"
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    await modal.on_submit(interaction)
    # Singleton must not have changed.
    assert current_phase_durations().vote == 60
    interaction.response.send_message.assert_called_once()
    interaction.response.edit_message.assert_not_called()


async def test_refresh_rejects_zero_or_negative() -> None:
    from wolfbot.ui.settings_view import _DurationEditModal

    view = PhaseDurationsView(host_user_id="host1")
    modal = _DurationEditModal(
        field_name="vote",
        label="投票",
        current_value=60,
        parent=view,
    )
    modal.value_input = MagicMock()
    modal.value_input.value = "0"
    interaction = MagicMock()
    interaction.user.id = "host1"
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    await modal.on_submit(interaction)
    assert current_phase_durations().vote == 60
    interaction.response.send_message.assert_called_once()


async def test_factor_modal_scales_all_fields() -> None:
    from wolfbot.ui.settings_view import _FactorModal

    set_phase_durations(PhaseDurations())  # known starting point
    before = current_phase_durations()
    view = PhaseDurationsView(host_user_id="host1")
    modal = _FactorModal(parent=view)
    modal.factor_input = MagicMock()
    modal.factor_input.value = "0.5"
    interaction = MagicMock()
    interaction.user.id = "host1"
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    await modal.on_submit(interaction)
    after = current_phase_durations()
    # Every field is halved (with the floor of 1).
    assert after.vote == max(1, round(before.vote * 0.5))
    assert after.night == max(1, round(before.night * 0.5))
    assert after.discussion_day1 == max(1, round(before.discussion_day1 * 0.5))


async def test_factor_modal_rejects_invalid_factor() -> None:
    from wolfbot.ui.settings_view import _FactorModal

    set_phase_durations(PhaseDurations())
    before_vote = current_phase_durations().vote
    view = PhaseDurationsView(host_user_id="host1")
    modal = _FactorModal(parent=view)
    modal.factor_input = MagicMock()
    modal.factor_input.value = "0"
    interaction = MagicMock()
    interaction.user.id = "host1"
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    await modal.on_submit(interaction)
    assert current_phase_durations().vote == before_vote
    interaction.response.send_message.assert_called_once()
