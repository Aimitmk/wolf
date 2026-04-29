"""Phase-duration configuration — runtime-mutable singleton.

The wolf game has 5 fixed-length phases plus 3 day-numbered discussion
durations. Historically these were module-level ``int`` constants in
:mod:`wolfbot.domain.state_machine` and a function in
:mod:`wolfbot.domain.rules`. Both still exist as backwards-compatible
re-exports (state_machine.py uses :pep:`562` ``__getattr__`` to lazy-
read the singleton; ``rules.day_discussion_duration`` delegates here).

This module is the single source of truth at runtime:

  - :class:`PhaseDurations` is a frozen dataclass — immutable values.
  - The process holds one *current* :class:`PhaseDurations` instance
    in this module's private ``_current`` slot.
  - :func:`current_phase_durations` returns the current instance.
  - :func:`set_phase_durations` swaps the slot atomically.
  - :func:`reset_phase_durations_to_defaults` is a test convenience.

The singleton design is intentional: phase durations are a process-wide
operational knob, not per-game state. One Master process serves one
guild's worth of games, and an admin who runs
``/wolf settings duration_factor 0.5`` would expect *all* future phase
deadlines to use the new value — including ones in already-running
games. The current phase's ``deadline_epoch`` (already written to the
DB) is untouched; the new value applies on the next phase transition.
For per-game overrides (a richer model that would let two games on the
same Master use different durations) a future change can add a
``phase_durations`` JSON column to the ``games`` table and have
``plan_*`` functions prefer that over the singleton — the dataclass is
designed so a row's value can be carried alongside.

Usage:

    # at boot (main.py)
    settings.apply_phase_durations()

    # in plan_*() (state_machine.py)
    new_deadline_epoch=now_epoch + current_phase_durations().vote

    # future UI command (slash command handler)
    set_phase_durations(replace(current_phase_durations(), vote=15))
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class PhaseDurations:
    """Seconds for each phase deadline. Frozen — swap, don't mutate.

    Defaults match the historical hardcoded values from
    :mod:`wolfbot.domain.state_machine` and
    :func:`wolfbot.domain.rules.day_discussion_duration`. Tests assert
    against these defaults, so a new field's default must keep that
    equivalence to avoid silent breakage.
    """

    vote: int = 60
    runoff: int = 60
    night: int = 90
    day_discussion_grace: int = 30
    runoff_speech_grace: int = 30
    discussion_day1: int = 300
    discussion_day2: int = 240
    discussion_day3plus: int = 180

    def discussion_for_day(self, day_number: int) -> int:
        """Length of the discussion phase for ``day_number``.

        Mirrors the historical
        :func:`wolfbot.domain.rules.day_discussion_duration` logic.
        """
        if day_number <= 0:
            raise ValueError("day_number must be >= 1 for discussion")
        if day_number == 1:
            return self.discussion_day1
        if day_number == 2:
            return self.discussion_day2
        return self.discussion_day3plus

    def with_factor(self, factor: float) -> PhaseDurations:
        """Return a new :class:`PhaseDurations` with every value scaled
        by ``factor``, clamped to a minimum of 1 second per phase.

        The clamp matters in mock / fast-test mode: a factor of 0.01
        applied to a 60-second VOTE_DURATION rounds to 0, which would
        make ``deadline_epoch == now`` and the engine's deadline-watcher
        would advance immediately, sometimes before the LLM submission
        loop can even fire. 1 second is short enough for fast iteration
        but still gives the loop a tick.
        """
        if factor <= 0:
            raise ValueError("factor must be > 0")

        def _scale(v: int) -> int:
            return max(1, round(v * factor))

        return PhaseDurations(
            vote=_scale(self.vote),
            runoff=_scale(self.runoff),
            night=_scale(self.night),
            day_discussion_grace=_scale(self.day_discussion_grace),
            runoff_speech_grace=_scale(self.runoff_speech_grace),
            discussion_day1=_scale(self.discussion_day1),
            discussion_day2=_scale(self.discussion_day2),
            discussion_day3plus=_scale(self.discussion_day3plus),
        )

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
    ) -> PhaseDurations:
        """Build a :class:`PhaseDurations` from environment variables.

        Resolution order, applied in this sequence so individual
        overrides win against the global factor:

        1. Start with the dataclass defaults.
        2. If ``WOLFBOT_PHASE_DURATION_FACTOR`` is set, scale all
           values by it (a single knob for "make everything N times
           faster" — the typical mock / fast-test use case).
        3. Per-phase overrides apply absolute values, replacing
           whatever the factor produced:
             - ``WOLFBOT_VOTE_DURATION``
             - ``WOLFBOT_RUNOFF_DURATION``
             - ``WOLFBOT_NIGHT_DURATION``
             - ``WOLFBOT_DAY_DISCUSSION_GRACE``
             - ``WOLFBOT_RUNOFF_SPEECH_GRACE``
             - ``WOLFBOT_DISCUSSION_DAY1``
             - ``WOLFBOT_DISCUSSION_DAY2``
             - ``WOLFBOT_DISCUSSION_DAY3PLUS``

        Each override must parse as a positive integer (seconds); a
        non-positive value or unparseable string raises ``ValueError``
        at load time so config errors fail fast at boot.
        """
        e = env if env is not None else os.environ
        d = cls()
        factor_raw = e.get("WOLFBOT_PHASE_DURATION_FACTOR")
        if factor_raw is not None and factor_raw.strip():
            try:
                factor = float(factor_raw)
            except ValueError as exc:
                raise ValueError(
                    f"WOLFBOT_PHASE_DURATION_FACTOR must be a number, got {factor_raw!r}"
                ) from exc
            d = d.with_factor(factor)

        overrides: dict[str, str] = {
            "vote": "WOLFBOT_VOTE_DURATION",
            "runoff": "WOLFBOT_RUNOFF_DURATION",
            "night": "WOLFBOT_NIGHT_DURATION",
            "day_discussion_grace": "WOLFBOT_DAY_DISCUSSION_GRACE",
            "runoff_speech_grace": "WOLFBOT_RUNOFF_SPEECH_GRACE",
            "discussion_day1": "WOLFBOT_DISCUSSION_DAY1",
            "discussion_day2": "WOLFBOT_DISCUSSION_DAY2",
            "discussion_day3plus": "WOLFBOT_DISCUSSION_DAY3PLUS",
        }
        applied: dict[str, int] = {}
        for field_name, env_name in overrides.items():
            raw = e.get(env_name)
            if raw is None or not raw.strip():
                continue
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(
                    f"{env_name} must be an integer, got {raw!r}"
                ) from exc
            if value <= 0:
                raise ValueError(f"{env_name} must be > 0, got {value}")
            applied[field_name] = value
        if applied:
            d = replace(d, **applied)
        return d


_current: PhaseDurations = PhaseDurations()


def current_phase_durations() -> PhaseDurations:
    """Return the active :class:`PhaseDurations` instance.

    This is the read API every ``plan_*`` transition function uses.
    Reading is just a singleton-slot access — no I/O, deterministic.
    """
    return _current


def set_phase_durations(durations: PhaseDurations) -> None:
    """Swap the active :class:`PhaseDurations` instance.

    Called once at boot from
    :meth:`wolfbot.config.MasterSettings.apply_phase_durations`, and
    intended to be the exact same hook a future
    ``/wolf settings duration_factor ...`` slash command would call to
    flip the value at runtime. The change applies on the *next* phase
    transition; the current phase's already-written ``deadline_epoch``
    is unchanged.
    """
    global _current
    _current = durations


def reset_phase_durations_to_defaults() -> None:
    """Test convenience — restore the singleton to ``PhaseDurations()``.

    Tests that mutate the singleton must restore it afterward, otherwise
    later tests in the same process inherit the override.
    """
    global _current
    _current = PhaseDurations()


__all__ = [
    "PhaseDurations",
    "current_phase_durations",
    "reset_phase_durations_to_defaults",
    "set_phase_durations",
]
