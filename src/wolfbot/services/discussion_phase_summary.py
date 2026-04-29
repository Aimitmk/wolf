"""Compute and emit the `discussion_phase_summary` event for a finished phase.

Aggregates counts from `speech_events` (human vs npc) and the npc-audit
tables (`npc_speak_*`, `npc_playback_events`) so the summary reflects the
full reactive_voice flow. Emit at the end of every public-speech phase
(`DAY_DISCUSSION`, `DAY_RUNOFF_SPEECH`) regardless of mode.
"""

from __future__ import annotations

import logging

from wolfbot.domain.discussion import SpeechSource
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discussion_service import DiscussionService
from wolfbot.services.structured_logging import (
    COMPONENT_MASTER,
    build_discussion_phase_summary,
    emit_event,
)

logger = logging.getLogger("wolfbot")


async def emit_phase_summary(
    *,
    repo: SqliteRepo,
    discussion: DiscussionService,
    game_id: str,
    phase_id: str,
    mode: str,
) -> dict[str, int]:
    """Read speech_events + npc_* audit rows, build + emit the summary.

    Returns the counts dict so tests can assert without re-parsing logs.
    """
    events = await discussion.load_phase(game_id, phase_id)
    human = sum(1 for e in events if e.source in (SpeechSource.TEXT, SpeechSource.VOICE_STT))
    npc = sum(1 for e in events if e.source == SpeechSource.NPC_GENERATED)
    total = human + npc  # phase_baseline excluded

    # Reactive-voice telemetry from audit tables. Counts come from joining
    # over the request_id; we do simple SQL aggregates here to avoid
    # loading every row.
    async with repo._db.execute(
        """
        SELECT
            COUNT(*) AS speak_requests_sent,
            (SELECT COUNT(*) FROM npc_speak_results
              WHERE game_id=? AND phase_id=? AND status='accepted')
                AS speak_results_accepted,
            (SELECT COUNT(*) FROM npc_speak_results
              WHERE game_id=? AND phase_id=? AND status='rejected')
                AS speak_results_rejected,
            (SELECT COUNT(*) FROM npc_playback_events
              WHERE game_id=? AND phase_id=?)
                AS playback_authorized,
            (SELECT COUNT(*) FROM npc_playback_events
              WHERE game_id=? AND phase_id=? AND tts_outcome='success')
                AS tts_success,
            (SELECT COUNT(*) FROM npc_playback_events
              WHERE game_id=? AND phase_id=? AND tts_outcome='failed')
                AS tts_failed,
            (SELECT COUNT(*) FROM npc_playback_events
              WHERE game_id=? AND phase_id=? AND outcome='succeeded')
                AS playback_success,
            (SELECT COUNT(*) FROM npc_playback_events
              WHERE game_id=? AND phase_id=? AND outcome='failed')
                AS playback_failed,
            (SELECT COUNT(*) FROM npc_speak_results
              WHERE game_id=? AND phase_id=? AND failure_reason IN ('stale_phase','expired_request','master_restart'))
                AS stale_dropped
        FROM npc_speak_requests
        WHERE game_id=? AND phase_id=?
        """,
        (
            game_id,
            phase_id,  # speak_results_accepted
            game_id,
            phase_id,  # speak_results_rejected
            game_id,
            phase_id,  # playback_authorized
            game_id,
            phase_id,  # tts_success
            game_id,
            phase_id,  # tts_failed
            game_id,
            phase_id,  # playback_success
            game_id,
            phase_id,  # playback_failed
            game_id,
            phase_id,  # stale_dropped
            game_id,
            phase_id,  # speak_requests_sent (outer)
        ),
    ) as cur:
        row = await cur.fetchone()

    aggregates: dict[str, int] = {}
    if row is not None:
        aggregates = {
            "speak_requests_sent": row[0] or 0,
            "speak_results_accepted": row[1] or 0,
            "speak_results_rejected": row[2] or 0,
            "playback_authorized": row[3] or 0,
            "tts_success": row[4] or 0,
            "tts_failed": row[5] or 0,
            "playback_success": row[6] or 0,
            "playback_failed": row[7] or 0,
            "stale_dropped": row[8] or 0,
        }

    payload = build_discussion_phase_summary(
        game_id=game_id,
        phase_id=phase_id,
        mode=mode,
        speech_events_total=total,
        human_speech_events=human,
        npc_speech_events=npc,
        **aggregates,
    )
    emit_event(
        logger,
        component=COMPONENT_MASTER,
        event="discussion_phase_summary",
        game_id=game_id,
        phase_id=phase_id,
        **{k: v for k, v in payload.items() if k not in ("game_id", "phase_id")},
    )
    return {
        "speech_events_total": total,
        "human_speech_events": human,
        "npc_speech_events": npc,
        **aggregates,
    }


__all__ = ["emit_phase_summary"]
