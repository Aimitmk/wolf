"""Master narration templates — Levi-style polite-machine announcements.

Translates a `LogEntry` (the raw text already produced by
`domain/state_machine.plan_*`) into a `NarrationOutput` that the
DiscordBotAdapter routes to:

  * `voice_text` — short string read aloud via VOICEVOX in the VC.
  * `chat_text`  — long string posted to the VC's attached text chat.

Either or both may be `None`. Rules:

  * Headline-level announcements (PHASE_CHANGE / MORNING / VICTORY /
    SETUP_COMPLETE / EXECUTION-headline / NO_EXECUTION-headline /
    RUNOFF_START-headline) are voiced. They are short by design.
  * Tally / reveal blobs (vote tally, ROLE_REVEAL) are too long to
    pleasantly voice — they go to the VC text chat untouched.
  * In `EXECUTION` / `NO_EXECUTION` / `RUNOFF_START` the existing log
    entry has the form ``"<headline>{tally_suffix}"`` where
    ``tally_suffix == f"\\n\\n{tally}"``. The narrator splits on the
    blank line: voice the headline (with Levi rewording), text-post the
    tally separately.
  * `ROLE_REVEAL` is text-only.
  * In rounds mode the narrator is bypassed entirely — existing
    `post_public` / `post_morning` text remains.

The persona's tone is hard-coded into the templates rather than fed
through an LLM; this keeps narration deterministic, cheap, and safe to
re-run on phase recovery without paying API tokens. The Levi persona
data in `master.personas` exists for future LLM-styled rewrites and to
document the voice character.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from wolfbot.domain.durations import current_phase_durations
from wolfbot.domain.enums import Phase
from wolfbot.domain.models import LogEntry, Seat
from wolfbot.llm.template import render_template


@dataclass(frozen=True)
class NarrationContext:
    """Extra game context the rendered template may need.

    Built once per `LogEntry` from the live `Game` + `Seat` table by
    `DiscordBotAdapter` before calling `render`. Pure data — no I/O.
    """

    day_number: int
    phase: Phase
    alive_count: int
    seats_by_no: dict[int, Seat]


@dataclass(frozen=True)
class NarrationOutput:
    """What to do with this LogEntry under reactive_voice mode."""

    voice_text: str | None = None
    chat_text: str | None = None

    def is_silent(self) -> bool:
        return self.voice_text is None and self.chat_text is None


def _split_headline_and_tally(text: str) -> tuple[str, str | None]:
    """Split ``"<headline>\\n\\n<tally>"`` into ``(headline, tally)``.

    Mirrors how :func:`state_machine._apply_execution` /
    :func:`state_machine.plan_day_vote_resolve` glue the two pieces
    with a blank line. Falls back to ``(text, None)`` when no blank
    line is present.
    """
    head, _, tail = text.partition("\n\n")
    head = head.strip()
    tail = tail.strip()
    return head, (tail or None)


def _seat_label(seats_by_no: dict[int, Seat], seat_no: int | None) -> str:
    """Human-readable seat label used inside narration text."""
    if seat_no is None:
        return "対象不明"
    seat = seats_by_no.get(seat_no)
    if seat is None:
        return f"席{seat_no}"
    # Strip emoji prefix from display_name for cleaner TTS readout.
    name = seat.display_name.lstrip()
    while name and not (name[0].isalnum() or "぀" <= name[0] <= "ヿ"):
        name = name[1:]
    name = name.strip() or seat.display_name
    return f"席{seat_no}の {name} 様"


# ---------------------------------------------------------------- per-kind templates


def _narrate_setup_complete(_entry: LogEntry, ctx: NarrationContext) -> NarrationOutput:
    return NarrationOutput(
        voice_text=render_template(
            "master/narration_setup_complete",
            alive_count=ctx.alive_count,
        ),
    )


def _narrate_phase_change(entry: LogEntry, ctx: NarrationContext) -> NarrationOutput:
    durations = current_phase_durations()
    target = entry.phase
    if target is Phase.DAY_DISCUSSION:
        secs = durations.discussion_for_day(max(1, ctx.day_number))
        return NarrationOutput(
            voice_text=render_template(
                "master/narration_phase_change_day_discussion",
                # Day 2+ skips the dawn line because MORNING already announced
                # it with casualty info; the template's two if-branches encode
                # this fork.
                is_first_day=ctx.day_number <= 1,
                is_later_day=ctx.day_number > 1,
                day_number=ctx.day_number,
                secs=secs,
            )
        )
    if target is Phase.DAY_VOTE:
        return NarrationOutput(
            voice_text=render_template(
                "master/narration_phase_change_day_vote",
                secs=durations.vote,
            )
        )
    if target is Phase.DAY_RUNOFF:
        return NarrationOutput(
            voice_text=render_template(
                "master/narration_phase_change_day_runoff",
                secs=durations.runoff,
            )
        )
    if target is Phase.DAY_RUNOFF_SPEECH:
        return NarrationOutput(
            voice_text=render_template(
                "master/narration_phase_change_day_runoff_speech",
                secs=durations.runoff_speech_grace,
            )
        )
    if target is Phase.NIGHT:
        return NarrationOutput(
            voice_text=render_template(
                "master/narration_phase_change_night",
                secs=durations.night,
            )
        )
    # Unknown / GAME_OVER fallthrough — voice the raw entry text as-is.
    return NarrationOutput(voice_text=entry.text)


def _narrate_morning(entry: LogEntry, ctx: NarrationContext) -> NarrationOutput:
    """MORNING entry → Levi reframing.

    Source text is e.g. "夜が明けました。席3 Bob が無残な姿で発見された..."
    The template rewrites it neutrally with day_number + casualty info.
    Two branches gated by has/no_casualty so the same template covers
    both "犠牲者なし" and "<seat> の停止を確認" without a Python ternary.
    """
    has_casualty = entry.actor_seat is not None
    target = (
        _seat_label(ctx.seats_by_no, entry.actor_seat) if has_casualty else ""
    )
    return NarrationOutput(
        voice_text=render_template(
            "master/narration_morning",
            day_number=ctx.day_number,
            has_casualty=has_casualty,
            no_casualty=not has_casualty,
            target=target,
            alive_count=ctx.alive_count,
        )
    )


def _narrate_execution(entry: LogEntry, ctx: NarrationContext) -> NarrationOutput:
    """EXECUTION entry → Levi confirmation + (optional) NIGHT cue.

    The state-machine path emits no PHASE_CHANGE log between the
    EXECUTION row and the NIGHT phase entry, so role-holders would
    otherwise get silent DMs without any "夜です" cue. The template's
    ``append_night_intro`` branch is on iff the next phase is actually
    NIGHT — when execution triggers a victory the game flips to
    GAME_OVER and the VICTORY narration takes over instead.
    """
    headline, tally = _split_headline_and_tally(entry.text)
    target = _seat_label(ctx.seats_by_no, entry.actor_seat)
    going_to_night = ctx.phase is Phase.NIGHT
    voice = render_template(
        "master/narration_execution",
        target=target,
        append_night_intro=going_to_night,
        night_secs=current_phase_durations().night if going_to_night else 0,
    )
    chat = tally if tally else None
    if chat is None and headline:
        chat = headline
    return NarrationOutput(voice_text=voice, chat_text=chat)


def _narrate_no_execution(entry: LogEntry, ctx: NarrationContext) -> NarrationOutput:
    headline, tally = _split_headline_and_tally(entry.text)
    is_runoff_tie = "決選投票も同票" in headline
    is_invalid_vote = "投票結果が無効" in headline
    voice = render_template(
        "master/narration_no_execution",
        is_runoff_tie=is_runoff_tie,
        is_invalid_vote=is_invalid_vote and not is_runoff_tie,
        is_default=not is_runoff_tie and not is_invalid_vote,
    )
    chat = tally if tally else None
    if chat is None and headline:
        chat = headline
    return NarrationOutput(voice_text=voice, chat_text=chat)


def _narrate_runoff_start(entry: LogEntry, _ctx: NarrationContext) -> NarrationOutput:
    headline, tally = _split_headline_and_tally(entry.text)
    voice = render_template("master/narration_runoff_start")
    # Keep the candidate list + tally as text so players can see who's tied.
    chat_parts: list[str] = []
    if headline:
        chat_parts.append(headline)
    if tally:
        chat_parts.append(tally)
    chat = "\n\n".join(chat_parts) if chat_parts else None
    return NarrationOutput(voice_text=voice, chat_text=chat)


def _narrate_victory(entry: LogEntry, _ctx: NarrationContext) -> NarrationOutput:
    """VICTORY entry → Levi end-of-game line.

    state_machine emits text like 村人陣営の勝利 or 人狼陣営の勝利 (with a
    fullwidth exclamation). Substring-match picks the template branch;
    anything unrecognised falls into the ``is_other`` branch which
    quotes the raw text verbatim — keeping a clean fallback for any
    future faction string the engine adds.
    """
    text = entry.text.strip()
    is_village = "村人" in text or "村陣営" in text
    is_wolf = (not is_village) and ("人狼" in text or "狼陣営" in text)
    is_other = not is_village and not is_wolf
    return NarrationOutput(
        voice_text=render_template(
            "master/narration_victory",
            is_village=is_village,
            is_wolf=is_wolf,
            is_other=is_other,
            raw_text=text,
        )
    )


def _narrate_role_reveal(entry: LogEntry, _ctx: NarrationContext) -> NarrationOutput:
    # 9-line role table — too long for TTS, post to chat verbatim.
    return NarrationOutput(chat_text=entry.text)


_NarrationHandler = Callable[[LogEntry, NarrationContext], NarrationOutput]

_HANDLERS: dict[str, _NarrationHandler] = {
    "SETUP_COMPLETE": _narrate_setup_complete,
    "PHASE_CHANGE": _narrate_phase_change,
    "MORNING": _narrate_morning,
    "EXECUTION": _narrate_execution,
    "NO_EXECUTION": _narrate_no_execution,
    "RUNOFF_START": _narrate_runoff_start,
    "VICTORY": _narrate_victory,
    "ROLE_REVEAL": _narrate_role_reveal,
}


def render_master_narration(
    entry: LogEntry, ctx: NarrationContext
) -> NarrationOutput:
    """Translate a public `LogEntry` into a Levi-styled narration.

    Returns ``NarrationOutput(None, None)`` when the entry kind has no
    narration template. Callers fall back to posting the entry's raw
    text in that case (no semantic loss vs the rounds-mode path).
    """
    handler = _HANDLERS.get(entry.kind)
    if handler is None:
        return NarrationOutput()
    return handler(entry, ctx)


def render_runoff_candidate_intro(seat: Seat) -> str:
    """Levi-styled introduction line for a runoff candidate's speech.

    Spoken by Master in VC right before SpeakArbiter dispatches the
    candidate's SpeakRequest, so listeners always know whose final
    speech is starting (the same way 「占い師の発表」 patterns name the
    speaker out loud). Uses the same emoji-stripping logic as
    ``_seat_label`` so the readout pronounces the persona name cleanly.

    Body: ``master/narration_runoff_candidate_intro.md``.
    """
    name = seat.display_name.lstrip()
    while name and not (name[0].isalnum() or "぀" <= name[0] <= "ヿ"):
        name = name[1:]
    name = name.strip() or seat.display_name
    return render_template(
        "master/narration_runoff_candidate_intro",
        name=name,
    )


__all__ = [
    "NarrationContext",
    "NarrationOutput",
    "render_master_narration",
    "render_runoff_candidate_intro",
]
