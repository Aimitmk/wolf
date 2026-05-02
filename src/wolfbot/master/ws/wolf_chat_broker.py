"""Master-side wolf-chat fan-out for Phase-D.

In `reactive_voice` mode, when a wolf NPC posts a coordination line via
`WolfChatSend`, Master:

1. Persists it as a private `WOLF_CHAT` `LogEntry` (visibility=`PRIVATE`,
   matching how the rounds-mode wolf-chat path writes the same log row)
   so post-game replay still has the canonical history.
2. Pushes a `private_state_update(kind=wolf_chat)` to every other live
   wolf seat's NPC bot, so each wolf's `NpcGameState.wolf_chat_history`
   stays in sync without the bots ever reading the Master DB.
3. Optionally writes the line into the wolves-only Discord channel via
   the existing message_poster, so a human wolf player still sees the
   coordination line.

Pure orchestration — no LLM calls and no decisions. The broker is
constructed with the registry + repo references and a `now_ms` clock;
its single public entry is `handle_wolf_chat_send`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from wolfbot.domain.enums import Role
from wolfbot.domain.models import LogEntry
from wolfbot.domain.ws_messages import WolfChatSend
from wolfbot.master.state.private_state import make_wolf_chat_update
from wolfbot.master.ws.npc_registry import NpcEntry, NpcRegistry
from wolfbot.persistence.sqlite_repo import SqliteRepo

log = logging.getLogger(__name__)


class WolfChatBroker:
    """Receive-and-fan-out for wolf chat lines from NPC bots.

    The broker is per-Master-process; routing is by ``game_id`` extracted
    from the `WolfChatSend` payload. Best-effort end-to-end — a single
    failed write/broadcast leg is logged but doesn't block the others.
    """

    def __init__(
        self,
        *,
        registry: NpcRegistry,
        repo: SqliteRepo,
        post_to_wolves_channel: Callable[[str, str], Awaitable[None]] | None = None,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.registry = registry
        self.repo = repo
        self._post_to_wolves_channel = post_to_wolves_channel
        self._now_ms = now_ms

    async def handle_wolf_chat_send(self, msg: WolfChatSend) -> None:
        # 1) Validate the sender is actually an alive wolf at this seat.
        try:
            game = await self.repo.load_game(msg.game_id)
            seats = await self.repo.load_seats(msg.game_id)
            players = await self.repo.load_players(msg.game_id)
        except Exception:
            log.exception(
                "wolf_chat_load_failed game=%s seat=%d",
                msg.game_id, msg.seat_no,
            )
            return
        if game is None or game.ended_at is not None:
            log.info(
                "wolf_chat_drop_no_game game=%s seat=%d",
                msg.game_id, msg.seat_no,
            )
            return
        sender = next((p for p in players if p.seat_no == msg.seat_no), None)
        if (
            sender is None
            or not sender.alive
            or sender.role is not Role.WEREWOLF
        ):
            log.info(
                "wolf_chat_drop_not_wolf game=%s seat=%d role=%s",
                msg.game_id, msg.seat_no,
                sender.role.value if sender and sender.role else "?",
            )
            return

        seats_by_no = {s.seat_no: s for s in seats}
        sender_name = (
            seats_by_no[msg.seat_no].display_name
            if msg.seat_no in seats_by_no
            else f"席{msg.seat_no}"
        )
        text = msg.text.strip()
        if not text:
            return

        # 2) Persist the canonical WOLF_CHAT log entry.
        try:
            await self.repo.insert_log_private(
                LogEntry(
                    game_id=msg.game_id,
                    day=game.day_number,
                    phase=game.phase,
                    kind="WOLF_CHAT",
                    actor_seat=msg.seat_no,
                    visibility="PRIVATE",
                    text=text,
                    created_at=int(self._now_ms() / 1000),
                )
            )
        except Exception:
            log.exception(
                "wolf_chat_log_insert_failed game=%s seat=%d",
                msg.game_id, msg.seat_no,
            )

        # 3) Fan out a PrivateStateUpdate(wolf_chat) to every OTHER live
        # wolf seat's NPC bot. The sender's own state is updated by its
        # own LLM-side bookkeeping; we don't echo back to avoid
        # double-recording the same line.
        recipients = [
            p
            for p in players
            if p.role is Role.WEREWOLF and p.alive and p.seat_no != msg.seat_no
        ]
        for recipient in recipients:
            entry = self._find_npc_for_seat(msg.game_id, recipient.seat_no)
            if entry is None or entry.send is None:
                continue
            update = make_wolf_chat_update(
                npc_id=entry.npc_id,
                game_id=msg.game_id,
                seat_no=recipient.seat_no,
                day=game.day_number,
                speaker_seat=msg.seat_no,
                speaker_name=sender_name,
                text=text,
                ts=self._now_ms(),
                trace_id=f"wolf_chat-{msg.game_id}-{msg.seat_no}",
            )
            try:
                await entry.send(update.model_dump_json())
            except Exception:
                log.exception(
                    "wolf_chat_broadcast_failed npc=%s seat=%d",
                    entry.npc_id, recipient.seat_no,
                )

        # 4) Mirror to the wolves-only Discord channel so a human wolf
        # (if any) sees the coordination line. Best-effort.
        if self._post_to_wolves_channel is not None:
            try:
                await self._post_to_wolves_channel(
                    msg.game_id, f"**{sender_name}** (狼チャット): {text}",
                )
            except Exception:
                log.exception(
                    "wolf_chat_channel_post_failed game=%s", msg.game_id,
                )

    def _find_npc_for_seat(self, game_id: str, seat_no: int) -> NpcEntry | None:
        for entry in self.registry.all_online():
            if entry.assigned_seat == seat_no and entry.game_id == game_id:
                return entry
        return None


__all__ = ["WolfChatBroker"]
