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
from typing import Protocol, runtime_checkable

from wolfbot.domain.discussion import (
    SpeechEvent,
    SpeechSource,
    make_phase_id,
)
from wolfbot.domain.enums import Phase
from wolfbot.domain.ws_messages import SpeechEventPayload
from wolfbot.services.discussion_service import (
    DiscussionService,
    new_event_id,
)
from wolfbot.services.discussion_service import (
    now_ms as default_now_ms,
)
from wolfbot.services.npc_registry import NpcRegistry

log = logging.getLogger(__name__)


@runtime_checkable
class PhaseLookup(Protocol):
    """Resolves the current phase / day / alive seats for a game id.

    Defined as a protocol so tests can substitute an in-memory map without
    spinning up `SqliteRepo`. The real implementation is a thin shim that
    delegates to the repo.
    """

    async def get_phase(self, game_id: str) -> tuple[Phase, int] | None: ...

    async def get_alive_seat_nos(self, game_id: str) -> list[int]: ...


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
        await self.discussion.begin_phase_if_absent(
            game_id=payload.game_id,
            day=day,
            phase=phase,
            alive_seat_nos=alive_seat_nos,
        )

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
            created_at_ms=default_now_ms(),
        )
        await self.discussion.record(event)
        return (event, None)


__all__ = ["MasterIngestService", "PhaseLookup"]
