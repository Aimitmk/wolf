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
        voice_text=(
            "本機は進行管理 LEVI でございます。"
            f"参加者 {ctx.alive_count} 名で、これより人狼ゲームを開始致します。"
            "役職の通知は各参加者の DM へ送付済みです。ご確認をお願い致します。"
        ),
    )


def _narrate_phase_change(entry: LogEntry, ctx: NarrationContext) -> NarrationOutput:
    durations = current_phase_durations()
    target = entry.phase
    if target is Phase.DAY_DISCUSSION:
        secs = durations.discussion_for_day(max(1, ctx.day_number))
        if ctx.day_number <= 1:
            voice = (
                "夜が明けました。1 日目の議論を開始致します。"
                f"制限時間は {secs} 秒でございます。"
            )
        else:
            # Day 2+: the MORNING entry already announced "夜が明けました" with
            # day_number + casualty info. Skip the duplicate dawn line here.
            voice = (
                f"{ctx.day_number} 日目の議論を開始致します。"
                f"制限時間は {secs} 秒でございます。"
            )
        return NarrationOutput(voice_text=voice)
    if target is Phase.DAY_VOTE:
        voice = (
            "議論時間が終了致しました。"
            f"投票フェイズへ移行致します。制限時間は {durations.vote} 秒でございます。"
            "投票は DM の選択 UI からお願い致します。"
        )
        return NarrationOutput(voice_text=voice)
    if target is Phase.DAY_RUNOFF:
        voice = (
            f"決選投票へ移行致します。制限時間は {durations.runoff} 秒でございます。"
            "DM の選択 UI から再度ご投票をお願い致します。"
        )
        return NarrationOutput(voice_text=voice)
    if target is Phase.DAY_RUNOFF_SPEECH:
        voice = (
            f"決選投票候補者の最終演説に移行致します。"
            f"演説時間は {durations.runoff_speech_grace} 秒でございます。"
        )
        return NarrationOutput(voice_text=voice)
    if target is Phase.NIGHT:
        voice = (
            f"夜のフェイズへ移行致します。制限時間は {durations.night} 秒でございます。"
            "役職を持つ参加者の方は、DM の選択 UI から行動をお願い致します。"
        )
        return NarrationOutput(voice_text=voice)
    # Unknown / GAME_OVER fallthrough — voice the raw entry text as-is.
    return NarrationOutput(voice_text=entry.text)


def _narrate_morning(entry: LogEntry, ctx: NarrationContext) -> NarrationOutput:
    # MORNING text has the form e.g. "夜が明けました。席3 Bob が無残な姿で発見された..."
    # Levi reframes it neutrally with day_number embedded.
    if entry.actor_seat is None:
        # No one died.
        voice = (
            f"夜が明けました。本日は {ctx.day_number} 日目でございます。"
            "本日、犠牲者は出ておりません。"
            f"現在の生存者は {ctx.alive_count} 名でございます。"
        )
    else:
        target = _seat_label(ctx.seats_by_no, entry.actor_seat)
        voice = (
            f"夜が明けました。本日は {ctx.day_number} 日目でございます。"
            f"昨夜、{target} の停止を確認致しました。"
            f"現在の生存者は {ctx.alive_count} 名でございます。"
        )
    return NarrationOutput(voice_text=voice)


def _narrate_execution(entry: LogEntry, ctx: NarrationContext) -> NarrationOutput:
    headline, tally = _split_headline_and_tally(entry.text)
    target = _seat_label(ctx.seats_by_no, entry.actor_seat)
    voice = f"{target} の処刑が確定致しました。"
    # The state-machine path emits no PHASE_CHANGE log between the EXECUTION
    # row and the NIGHT phase entry, so role-holders would otherwise get
    # silent DMs without any "夜です" cue. Append the same line that
    # `_narrate_phase_change` would have voiced for Phase.NIGHT — but only
    # when the next phase is actually NIGHT (when execution triggers a
    # victory the game flips to GAME_OVER and the VICTORY narration takes
    # over instead).
    if ctx.phase is Phase.NIGHT:
        durations = current_phase_durations()
        voice += (
            f"夜のフェイズへ移行致します。制限時間は {durations.night} 秒でございます。"
            "役職を持つ参加者の方は、DM の選択 UI から行動をお願い致します。"
        )
    # Strip the headline from the chat post — we voice it. Keep the tally so
    # the audit trail of who voted whom stays in the channel.
    chat = tally if tally else None
    # Headline alone is silent in chat (we already said it). If the entry
    # text contained ONLY the headline (no tally) we still want a chat
    # record for post-mortem; in practice all execution rows carry a tally.
    if chat is None and headline:
        chat = headline
    return NarrationOutput(voice_text=voice, chat_text=chat)


def _narrate_no_execution(entry: LogEntry, ctx: NarrationContext) -> NarrationOutput:
    headline, tally = _split_headline_and_tally(entry.text)
    voice = "有効な投票が成立致しませんでした。本日は処刑なしで夜のフェイズへ移行致します。"
    if "決選投票も同票" in headline:
        voice = "決選投票も同票となりました。本日は処刑なしで夜のフェイズへ移行致します。"
    elif "投票結果が無効" in headline:
        voice = "投票結果が無効でございました。本日は処刑なしで夜のフェイズへ移行致します。"
    chat = tally if tally else None
    if chat is None and headline:
        chat = headline
    return NarrationOutput(voice_text=voice, chat_text=chat)


def _narrate_runoff_start(entry: LogEntry, _ctx: NarrationContext) -> NarrationOutput:
    headline, tally = _split_headline_and_tally(entry.text)
    voice = (
        "投票が同数となりました。決選投票へ移行致します。候補者については本機からの追って案内をご確認ください。"
    )
    # Keep the candidate list + tally as text so players can see who's tied.
    chat_parts: list[str] = []
    if headline:
        chat_parts.append(headline)
    if tally:
        chat_parts.append(tally)
    chat = "\n\n".join(chat_parts) if chat_parts else None
    return NarrationOutput(voice_text=voice, chat_text=chat)


def _narrate_victory(entry: LogEntry, _ctx: NarrationContext) -> NarrationOutput:
    # state_machine emits VICTORY text like 村人陣営の勝利 or 人狼陣営の勝利
    # (with a fullwidth exclamation). Reframe it in Levi's tone.
    text = entry.text.strip()
    if "村人" in text or "村陣営" in text:
        voice = "判定致します。村人陣営の勝利でございます。ゲームを終了致します。"
    elif "人狼" in text or "狼陣営" in text:
        voice = "判定致します。人狼陣営の勝利でございます。ゲームを終了致します。"
    else:
        voice = f"{text}。ゲームを終了致します。"
    return NarrationOutput(voice_text=voice)


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
    """
    name = seat.display_name.lstrip()
    while name and not (name[0].isalnum() or "぀" <= name[0] <= "ヿ"):
        name = name[1:]
    name = name.strip() or seat.display_name
    return (
        f"続いて、{name} 様の最終演説でございます。どうぞ。"
    )


__all__ = [
    "NarrationContext",
    "NarrationOutput",
    "render_master_narration",
    "render_runoff_candidate_intro",
]
