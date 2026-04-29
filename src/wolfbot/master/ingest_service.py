"""Master-side ingestion of SpeechEvent payloads from voice-ingest.

Handles the boundary where Discord audio becomes a `SpeechEvent`:

1. Voice-ingest sends a typed ``speech_event_payload`` over WS.
2. ``MasterIngestService.ingest_voice`` validates the speaker is NOT a
   registered NPC bot (the "fail-closed STT discard" rule from design.md),
   builds a `SpeechEvent(source=voice_stt)`, and delegates to
   `DiscussionService.record(...)` so the canonical PLAYER_SPEECH log entry
   and main-channel post are emitted.

Keeping this in its own service (rather than inline in the WS handler) makes
the path easy to test with a Fake WS context and easy to call from the human-
text-message capture path on `WolfCog` (which also produces SpeechEvents).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from wolfbot.domain.discussion import (
    SpeechEvent,
    SpeechSource,
    make_phase_id,
)
from wolfbot.domain.enums import Phase
from wolfbot.domain.models import Seat
from wolfbot.domain.ws_messages import SpeechEventPayload
from wolfbot.master.npc_registry import NpcRegistry
from wolfbot.services.discussion_service import (
    DiscussionService,
    new_event_id,
)
from wolfbot.services.discussion_service import (
    now_ms as default_now_ms,
)

log = logging.getLogger(__name__)


_HONORIFICS: tuple[str, ...] = ("さん", "くん", "ちゃん", "様", "さま", "君")


def _strip_emoji(name: str) -> str:
    """Drop leading emoji + whitespace; persona display_names are like '🌙セツ'."""
    out: list[str] = []
    skipping = True
    for ch in name:
        if skipping:
            if ch.isspace():
                continue
            cat = unicodedata.category(ch)
            # Symbols (S*) and 'Cn' (unassigned) covers most emoji codepoints.
            if cat.startswith("S") or cat == "Cn":
                continue
            skipping = False
        out.append(ch)
    return "".join(out).strip()


def _normalize_name(name: str) -> str:
    """Lowercase + NFKC + strip whitespace, emoji, and trailing honorifics.

    Used for both the spoken alias and the seat display name so the
    comparison is symmetric and forgiving.
    """
    folded = unicodedata.normalize("NFKC", name).strip().lower()
    folded = _strip_emoji(folded)
    for honorific in _HONORIFICS:
        if folded.endswith(honorific):
            folded = folded[: -len(honorific)].rstrip()
            break
    return folded


_SEAT_NUMBER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^席?\s*(\d+)\s*(?:番|ばん)?$"),
    re.compile(r"^seat\s*(\d+)$"),
)


def resolve_seat_by_name(
    addressed_name: str,
    seats: Iterable[Seat],
    alive: frozenset[int] | set[int] | None = None,
) -> int | None:
    """Map ``addressed_name`` (literal handle from voice) to a seat number.

    Returns ``None`` when no seat matches, or when ``alive`` is provided and
    the candidate is not currently alive (we never route an address at a
    dead seat — they cannot reply). Ambiguous matches across multiple seats
    also return ``None`` to fail closed.
    """
    norm = _normalize_name(addressed_name)
    if not norm:
        return None

    for pat in _SEAT_NUMBER_PATTERNS:
        m = pat.match(norm)
        if m:
            try:
                seat_no = int(m.group(1))
            except ValueError:
                continue
            if 1 <= seat_no <= 9 and (alive is None or seat_no in alive):
                return seat_no
            return None

    matches: set[int] = set()
    for seat in seats:
        if alive is not None and seat.seat_no not in alive:
            continue
        candidates = {_normalize_name(seat.display_name)}
        if seat.persona_key:
            candidates.add(_normalize_name(seat.persona_key))
        candidates.discard("")
        if norm in candidates:
            matches.add(seat.seat_no)
            continue
        # Fall back to substring containment in either direction so handles
        # like "ジナ" still match a display_name "ジナ・メイユイ".
        for cand in candidates:
            if cand and (norm == cand or norm in cand or cand in norm):
                matches.add(seat.seat_no)
                break

    if len(matches) == 1:
        return matches.pop()
    return None


@runtime_checkable
class PhaseLookup(Protocol):
    """Resolves the current phase / day / alive seats for a game id.

    Defined as a protocol so tests can substitute an in-memory map without
    spinning up `SqliteRepo`. The real implementation is a thin shim that
    delegates to the repo.
    """

    async def get_phase(self, game_id: str) -> tuple[Phase, int] | None: ...

    async def get_alive_seat_nos(self, game_id: str) -> list[int]: ...

    async def resolve_addressed_seat(
        self, game_id: str, addressed_name: str
    ) -> int | None:
        """Map a literal name/handle ('セツ', '席3', 'Alice さん') to a seat
        number for `game_id`. Returns ``None`` when no seat matches or the
        match is ambiguous."""
        ...


class MasterIngestService:
    """Boundary handler for voice-ingest payloads."""

    def __init__(
        self,
        *,
        registry: NpcRegistry,
        discussion: DiscussionService,
        phase_lookup: PhaseLookup,
    ) -> None:
        self.registry = registry
        self.discussion = discussion
        self.phase_lookup = phase_lookup

    async def ingest_voice(
        self, payload: SpeechEventPayload
    ) -> tuple[SpeechEvent | None, str | None]:
        """Try to record a `SpeechEvent(source=voice_stt)`.

        Returns ``(event, None)`` on success and ``(None, reason)`` when the
        payload is dropped. Reasons are the canonical voice-ingest /
        Master-side `failure_reason` values (`npc_stt_discarded`,
        `stale_phase`, `unknown_game`).
        """
        npc_user_ids = self.registry.discord_bot_user_ids()
        if payload.speaker_discord_user_id in npc_user_ids:
            log.info(
                "npc_stt_discarded game=%s speaker=%s segment=%s",
                payload.game_id,
                payload.speaker_discord_user_id,
                payload.segment_id,
            )
            return (None, "npc_stt_discarded")

        phase_info = await self.phase_lookup.get_phase(payload.game_id)
        if phase_info is None:
            log.info("voice_ingest_unknown_game game=%s", payload.game_id)
            return (None, "unknown_game")
        phase, day = phase_info
        if phase not in (Phase.DAY_DISCUSSION, Phase.DAY_RUNOFF_SPEECH):
            log.info(
                "voice_ingest_stale_phase game=%s phase=%s",
                payload.game_id,
                phase,
            )
            return (None, "stale_phase")

        # Compute the canonical phase_id on Master rather than trusting
        # the caller-supplied value. This ensures a delayed STT result
        # from an older phase cannot be written under a stale phase_id.
        canonical_phase_id = make_phase_id(payload.game_id, day, phase)

        # Seed the phase baseline so PublicDiscussionState rebuild works.
        alive_seat_nos = await self.phase_lookup.get_alive_seat_nos(payload.game_id)

        # Defensive alive check. The voice-ingest VAD entry already
        # filters dead-player audio (their seat is dropped from the
        # seat lookup map), and Discord-level VC mute is applied on
        # death — but a delayed STT result could still arrive after a
        # player died mid-segment. Drop it here so a dead player can
        # never inject SpeechEvents into the public log.
        if payload.seat_no not in set(alive_seat_nos):
            log.info(
                "dead_speaker_stt_discarded game=%s seat=%d segment=%s",
                payload.game_id,
                payload.seat_no,
                payload.segment_id,
            )
            return (None, "dead_speaker_discarded")

        await self.discussion.begin_phase_if_absent(
            game_id=payload.game_id,
            day=day,
            phase=phase,
            alive_seat_nos=alive_seat_nos,
        )

        # Prefer the analyzer's pre-resolved seat number when its
        # prompt was grounded with a roster - that path bypasses the
        # legacy ``resolve_seat_by_name`` string match, which only
        # compares against ``Seat.display_name`` (=persona handle)
        # and silently drops the address when the live VC nickname
        # diverges. Validate the seat is actually alive in this
        # phase before trusting it; fall back to the name-resolution
        # path otherwise so a buggy / hallucinated seat number
        # doesn't poison the routing.
        addressed_seat_no: int | None = None
        alive_set = set(alive_seat_nos)
        if (
            payload.addressed_seat_no is not None
            and payload.addressed_seat_no in alive_set
        ):
            addressed_seat_no = payload.addressed_seat_no
        elif payload.addressed_name:
            try:
                addressed_seat_no = await self.phase_lookup.resolve_addressed_seat(
                    payload.game_id, payload.addressed_name
                )
            except Exception:
                log.exception(
                    "addressed_seat_resolution_failed game=%s name=%s",
                    payload.game_id,
                    payload.addressed_name,
                )
                addressed_seat_no = None
        # Self-address never needs a routed reply.
        if addressed_seat_no is not None and addressed_seat_no == payload.seat_no:
            addressed_seat_no = None

        event = SpeechEvent(
            event_id=new_event_id(),
            game_id=payload.game_id,
            phase_id=canonical_phase_id,
            day=day,
            phase=phase,
            source=SpeechSource.VOICE_STT,
            speaker_kind="human",  # type: ignore[arg-type]
            speaker_seat=payload.seat_no,
            text=payload.text,
            stt_confidence=payload.confidence,
            audio_start_ms=payload.audio_start_ms,
            audio_end_ms=payload.audio_end_ms,
            summary=payload.summary,
            co_declaration=payload.co_declaration,
            addressed_seat_no=addressed_seat_no,
            role_callout=payload.role_callout,
            created_at_ms=default_now_ms(),
        )
        await self.discussion.record(event)
        return (event, None)


__all__ = ["MasterIngestService", "PhaseLookup", "resolve_seat_by_name"]
