"""Master narration templates + Levi persona consolidation.

Pins the per-kind narration shape so future tone tweaks don't silently
strip variables that voice depends on (e.g. day_number, discussion
seconds). Also asserts the persona pool is now a single Levi entry —
the 3-persona pool was never wired anywhere, so consolidating to 1
should not break anything else.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from wolfbot.domain.durations import (
    PhaseDurations,
    reset_phase_durations_to_defaults,
    set_phase_durations,
)
from wolfbot.domain.enums import Phase
from wolfbot.domain.models import LogEntry, Seat
from wolfbot.master.narration.narration import (
    NarrationContext,
    NarrationOutput,
    render_master_narration,
)
from wolfbot.master.personas import (
    DEFAULT_MASTER_PERSONA_KEY,
    MASTER_PERSONAS,
    MASTER_PERSONAS_BY_KEY,
)


@pytest.fixture(autouse=True)
def _reset_durations() -> None:
    reset_phase_durations_to_defaults()


def _seats() -> dict[int, Seat]:
    return {
        1: Seat(seat_no=1, display_name="🌙セツ", discord_user_id=None, is_llm=True, persona_key="setsu"),
        2: Seat(seat_no=2, display_name="🟣ジナ", discord_user_id=None, is_llm=True, persona_key="gina"),
        3: Seat(seat_no=3, display_name="Alice", discord_user_id="u3", is_llm=False, persona_key=None),
    }


def _ctx(*, day: int = 1, alive: int = 9, phase: Phase = Phase.DAY_DISCUSSION) -> NarrationContext:
    return NarrationContext(
        day_number=day,
        phase=phase,
        alive_count=alive,
        seats_by_no=_seats(),
    )


def _entry(kind: str, *, text: str = "", actor_seat: int | None = None, phase: Phase = Phase.DAY_DISCUSSION, day: int = 1) -> LogEntry:
    return LogEntry(
        game_id="g1",
        day=day,
        phase=phase,
        kind=kind,
        actor_seat=actor_seat,
        visibility="PUBLIC",
        text=text,
        created_at=0,
    )


# ------------------------------------------------------ persona consolidation


def test_master_persona_pool_is_just_levi() -> None:
    """The legacy 3-persona pool has been consolidated. Future code that
    iterates `MASTER_PERSONAS` should still work but only see one entry."""
    assert len(MASTER_PERSONAS) == 1
    assert DEFAULT_MASTER_PERSONA_KEY == "levi"
    assert "levi" in MASTER_PERSONAS_BY_KEY
    assert "stoic_gm" not in MASTER_PERSONAS_BY_KEY
    assert "theatrical_gm" not in MASTER_PERSONAS_BY_KEY
    assert "warm_gm" not in MASTER_PERSONAS_BY_KEY


def test_levi_persona_polite_machine_signals() -> None:
    """Sanity-check the Levi persona text carries the polite-machine
    signature so a future style edit doesn't silently turn it casual."""
    levi = MASTER_PERSONAS_BY_KEY["levi"]
    assert "丁寧" in levi.style_guide or "機械" in levi.style_guide
    assert levi.speech_profile.first_person == "本機"
    assert any("致します" in p for p in levi.speech_profile.signature_phrases)


# ------------------------------------------------------ phase change templates


def test_phase_change_to_day_discussion_day1_voices_duration() -> None:
    entry = _entry("PHASE_CHANGE", text="…", phase=Phase.DAY_DISCUSSION, day=1)
    out = render_master_narration(entry, _ctx(day=1))
    assert out.voice_text is not None
    assert "1 日目" in out.voice_text
    # Default discussion_day1 = 300 seconds.
    assert "300 秒" in out.voice_text
    assert out.chat_text is None


def test_phase_change_uses_runtime_durations() -> None:
    """Embedded durations must come from the runtime singleton, not
    hardcoded — otherwise `/wolf settings` changes don't reach voice."""
    set_phase_durations(replace(PhaseDurations(), discussion_day1=42))
    entry = _entry("PHASE_CHANGE", text="…", phase=Phase.DAY_DISCUSSION, day=1)
    out = render_master_narration(entry, _ctx(day=1))
    assert out.voice_text is not None
    assert "42 秒" in out.voice_text


def test_phase_change_to_day_vote_voices_explicit_end() -> None:
    """User explicitly asked for a 'discussion has ended' announcement.
    The DAY_VOTE entry must voice both that the discussion ended and
    that voting is starting."""
    entry = _entry("PHASE_CHANGE", text="…", phase=Phase.DAY_VOTE)
    out = render_master_narration(entry, _ctx())
    assert out.voice_text is not None
    assert "議論時間が終了" in out.voice_text
    assert "投票" in out.voice_text


def test_phase_change_to_night_voices_duration() -> None:
    entry = _entry("PHASE_CHANGE", text="…", phase=Phase.NIGHT)
    out = render_master_narration(entry, _ctx())
    assert out.voice_text is not None
    assert "夜のフェイズ" in out.voice_text
    assert "90 秒" in out.voice_text  # default night = 90s


def test_phase_change_day_discussion_day_three_uses_day3plus() -> None:
    set_phase_durations(replace(PhaseDurations(), discussion_day3plus=99))
    entry = _entry("PHASE_CHANGE", text="…", phase=Phase.DAY_DISCUSSION, day=3)
    out = render_master_narration(entry, _ctx(day=3))
    assert out.voice_text is not None
    assert "3 日目" in out.voice_text
    assert "99 秒" in out.voice_text


# ------------------------------------------------------ morning template


def test_morning_with_death_names_seat_and_player() -> None:
    entry = _entry("MORNING", text="夜が明け…", actor_seat=1, day=2, phase=Phase.DAY_DISCUSSION)
    out = render_master_narration(entry, _ctx(day=2, alive=8))
    assert out.voice_text is not None
    assert "2 日目" in out.voice_text
    assert "席1" in out.voice_text
    assert "セツ" in out.voice_text
    assert "8 名" in out.voice_text


def test_morning_without_death_voices_no_casualties() -> None:
    entry = _entry("MORNING", text="夜が明け…", actor_seat=None, day=2)
    out = render_master_narration(entry, _ctx(day=2, alive=9))
    assert out.voice_text is not None
    assert "犠牲者は出ておりません" in out.voice_text
    assert "9 名" in out.voice_text


# ------------------------------------------------------ vote-result split


def test_execution_voices_only_headline_chat_keeps_tally() -> None:
    """The user wants long content (vote tally) text-only, headline TTS."""
    full_text = "席1 セツ が処刑されました。\n\n席1: セツ → 席3 Alice\n席2: ジナ → 席1 セツ"
    entry = _entry("EXECUTION", text=full_text, actor_seat=1)
    out = render_master_narration(entry, _ctx())
    assert out.voice_text is not None
    assert "処刑" in out.voice_text
    assert "セツ" in out.voice_text
    # Tally must NOT be in voice (long content rule).
    assert "席1: セツ → 席3 Alice" not in out.voice_text
    # Tally must be in chat post.
    assert out.chat_text is not None
    assert "席1: セツ → 席3 Alice" in out.chat_text


def test_execution_voice_appends_night_phase_cue_when_next_phase_is_night() -> None:
    """state_machine emits no PHASE_CHANGE between EXECUTION and NIGHT entry,
    so the EXECUTION narration must announce the night transition itself —
    otherwise role-holders get DMs with no spoken context that night began.
    """
    full_text = "席1 セツ が処刑されました。\n\n席1: セツ → 席3 Alice"
    entry = _entry("EXECUTION", text=full_text, actor_seat=1)
    out = render_master_narration(entry, _ctx(phase=Phase.NIGHT))
    assert out.voice_text is not None
    assert "処刑が確定" in out.voice_text
    assert "夜のフェイズへ移行" in out.voice_text
    assert "役職を持つ参加者" in out.voice_text


def test_execution_voice_omits_night_cue_on_victory() -> None:
    """When execution triggers victory the live game.phase has flipped to
    GAME_OVER; the VICTORY narration takes over and the EXECUTION line must
    NOT also announce a night transition that will never happen."""
    full_text = "席1 セツ が処刑されました。\n\n席1: セツ → 席3 Alice"
    entry = _entry("EXECUTION", text=full_text, actor_seat=1)
    out = render_master_narration(entry, _ctx(phase=Phase.GAME_OVER))
    assert out.voice_text is not None
    assert "処刑が確定" in out.voice_text
    assert "夜のフェイズへ移行" not in out.voice_text


def test_no_execution_with_runoff_tie_branches_voice_text() -> None:
    full_text = "決選投票も同票のため、本日は処刑なしで夜を迎えます。\n\n席1: 棄権"
    entry = _entry("NO_EXECUTION", text=full_text)
    out = render_master_narration(entry, _ctx())
    assert out.voice_text is not None
    assert "決選投票も同票" in out.voice_text
    assert out.chat_text is not None
    assert "席1: 棄権" in out.chat_text


def test_runoff_start_keeps_candidate_list_in_chat() -> None:
    full_text = "同票のため決選投票に移ります。候補: セツ、ジナ\n\n席1: 1票"
    entry = _entry("RUNOFF_START", text=full_text)
    out = render_master_narration(entry, _ctx())
    assert out.voice_text is not None
    assert "決選投票" in out.voice_text
    # The candidate list and tally both go to chat.
    assert out.chat_text is not None
    assert "候補" in out.chat_text
    assert "席1: 1票" in out.chat_text


# ------------------------------------------------------ victory + reveal


def test_victory_villager_voiced() -> None:
    entry = _entry("VICTORY", text="村人陣営の勝利！")
    out = render_master_narration(entry, _ctx())
    assert out.voice_text is not None
    assert "村人陣営の勝利" in out.voice_text


def test_victory_wolf_voiced() -> None:
    entry = _entry("VICTORY", text="人狼陣営の勝利！")
    out = render_master_narration(entry, _ctx())
    assert out.voice_text is not None
    assert "人狼陣営の勝利" in out.voice_text


def test_role_reveal_is_chat_only() -> None:
    """ROLE_REVEAL is a 9-line table — too long for TTS by user choice."""
    full_text = (
        "席1 セツ: 占い師 (生存)\n席2 ジナ: 人狼 (生存)\n席3 Alice: 村人 (死亡)"
    )
    entry = _entry("ROLE_REVEAL", text=full_text)
    out = render_master_narration(entry, _ctx())
    assert out.voice_text is None
    assert out.chat_text == full_text


# ------------------------------------------------------ setup + fallthrough


def test_setup_complete_voiced_with_alive_count() -> None:
    entry = _entry("SETUP_COMPLETE", text="…")
    out = render_master_narration(entry, _ctx(alive=9))
    assert out.voice_text is not None
    assert "9 名" in out.voice_text
    assert "DM" in out.voice_text
    assert out.chat_text is None


def test_unknown_kind_returns_silent_output() -> None:
    """Unknown kinds fall through silently — caller posts raw text via
    legacy main-text path so nothing is lost."""
    entry = _entry("WHATEVER_NEW_KIND", text="…")
    out = render_master_narration(entry, _ctx())
    assert isinstance(out, NarrationOutput)
    assert out.is_silent()
