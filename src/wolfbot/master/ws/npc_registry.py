"""In-memory NPC bot registry maintained by Master.

Tracks every connected NPC bot's `npc_id`, Discord identity, assigned seat,
heartbeat freshness, and back-channel send target. Voice-ingest reads the
`discord_bot_user_id` set from this registry to filter NPC TTS audio out of
the STT pipeline (see voice-ingest spec).

Lifecycle:
- `register(...)` is called when a connecting NPC presents a valid
  `npc_register` payload over WebSocket.
- `heartbeat(npc_id, ts)` is called on every heartbeat message.
- `prune_offline(now_ts, timeout_ms)` is called periodically (or by the
  registry itself) to mark stale NPCs offline; offline NPCs are NOT removed
  from the registry — `is_online` flips to False so the arbiter can skip
  them, and reconnection re-flips to True via the next register call.
- `unregister(npc_id, reason)` is the explicit teardown path; emits the
  registry_update needed by voice-ingest.

Thread/asyncio model:
- The registry is intentionally single-threaded. Master is async-first; all
  operations run on the same event loop, so plain dicts are safe without a
  lock.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass
class NpcEntry:
    """One row in the registry."""

    npc_id: str
    discord_bot_user_id: str
    persona_key: str
    supported_voices: tuple[str, ...]
    version: str
    assigned_seat: int | None = None
    game_id: str | None = None
    phase_id: str | None = None
    last_heartbeat_ms: int = 0
    is_online: bool = True
    # Back-channel send target. Each NPC connection registers a coroutine
    # that takes a JSON-encoded message and writes it to the WS. Tests can
    # substitute a list-appender; real code wires this from MasterWsServer.
    send: Callable[[str], Awaitable[None]] | None = field(default=None, repr=False)


@runtime_checkable
class NpcRegistry(Protocol):
    """Protocol for testability — Fakes substitute as needed."""

    def register(
        self,
        *,
        npc_id: str,
        discord_bot_user_id: str,
        persona_key: str,
        supported_voices: tuple[str, ...],
        version: str,
        send: Callable[[str], Awaitable[None]] | None,
        now_ms: int,
    ) -> NpcEntry: ...

    def unregister(self, npc_id: str, reason: str) -> None: ...

    def heartbeat(self, npc_id: str, ts: int) -> None: ...

    def get(self, npc_id: str) -> NpcEntry | None: ...

    def all_online(self) -> list[NpcEntry]: ...

    def discord_bot_user_ids(self) -> set[str]: ...

    def prune_offline(self, now_ms: int, timeout_ms: int) -> list[str]: ...

    def assign(
        self,
        npc_id: str,
        *,
        seat: int,
        game_id: str,
        phase_id: str,
    ) -> None: ...

    def unassign(self, npc_id: str) -> None: ...

    def assigned_to_game(self, game_id: str) -> list[NpcEntry]: ...


class InMemoryNpcRegistry:
    """The default registry — production and test both use this."""

    def __init__(self) -> None:
        self._entries: dict[str, NpcEntry] = {}
        # Subscribers receive (added: set[str], removed: set[str]) of
        # discord_bot_user_id deltas; voice-ingest connections register here
        # to receive registry_update pushes.
        self._listeners: list[Callable[[set[str], set[str]], Awaitable[None]]] = []
        # Strong references to scheduled listener tasks so the GC does not
        # drop them mid-flight (ruff RUF006).
        self._listener_tasks: set[object] = set()

    # ---------------------------------------------------------- registration

    def register(
        self,
        *,
        npc_id: str,
        discord_bot_user_id: str,
        persona_key: str,
        supported_voices: tuple[str, ...],
        version: str,
        send: Callable[[str], Awaitable[None]] | None,
        now_ms: int,
    ) -> NpcEntry:
        previous = self._entries.get(npc_id)
        previous_uid = previous.discord_bot_user_id if previous is not None else None

        entry = NpcEntry(
            npc_id=npc_id,
            discord_bot_user_id=discord_bot_user_id,
            persona_key=persona_key,
            supported_voices=supported_voices,
            version=version,
            assigned_seat=previous.assigned_seat if previous is not None else None,
            game_id=previous.game_id if previous is not None else None,
            phase_id=previous.phase_id if previous is not None else None,
            last_heartbeat_ms=now_ms,
            is_online=True,
            send=send,
        )
        self._entries[npc_id] = entry

        added: set[str] = set()
        removed: set[str] = set()
        if previous_uid is None:
            added.add(discord_bot_user_id)
        elif previous_uid != discord_bot_user_id:
            removed.add(previous_uid)
            added.add(discord_bot_user_id)
        if added or removed:
            self._notify_listeners_sync(added, removed)
        return entry

    def unregister(self, npc_id: str, reason: str) -> None:
        entry = self._entries.pop(npc_id, None)
        if entry is None:
            return
        log.info("npc_unregister npc_id=%s reason=%s", npc_id, reason)
        self._notify_listeners_sync(set(), {entry.discord_bot_user_id})

    # ---------------------------------------------------------- heartbeat

    def heartbeat(self, npc_id: str, ts: int) -> None:
        entry = self._entries.get(npc_id)
        if entry is None:
            return
        entry.last_heartbeat_ms = ts
        if not entry.is_online:
            entry.is_online = True
            log.info("npc_recovered npc_id=%s", npc_id)

    # ---------------------------------------------------------- queries

    def get(self, npc_id: str) -> NpcEntry | None:
        return self._entries.get(npc_id)

    def all_online(self) -> list[NpcEntry]:
        return [e for e in self._entries.values() if e.is_online]

    def discord_bot_user_ids(self) -> set[str]:
        return {e.discord_bot_user_id for e in self._entries.values()}

    # ---------------------------------------------------------- pruning

    def prune_offline(self, now_ms: int, timeout_ms: int) -> list[str]:
        """Mark stale NPCs offline; return the list of just-marked-offline ids.

        Stale = `now_ms - last_heartbeat_ms > timeout_ms`. Only flips state once
        per stay-offline window; subsequent prunes return an empty list until
        the NPC reconnects and goes stale again.
        """
        marked: list[str] = []
        for npc_id, entry in self._entries.items():
            if entry.is_online and now_ms - entry.last_heartbeat_ms > timeout_ms:
                entry.is_online = False
                marked.append(npc_id)
        if marked:
            log.info("npc_offline_marked count=%d", len(marked))
        return marked

    # ---------------------------------------------------------- assignment

    def assign(
        self,
        npc_id: str,
        *,
        seat: int,
        game_id: str,
        phase_id: str,
    ) -> None:
        entry = self._entries.get(npc_id)
        if entry is None:
            return
        entry.assigned_seat = seat
        entry.game_id = game_id
        entry.phase_id = phase_id

    def unassign(self, npc_id: str) -> None:
        """Clear assignment fields for one NPC (game ended / never picked)."""
        entry = self._entries.get(npc_id)
        if entry is None:
            return
        entry.assigned_seat = None
        entry.game_id = None
        entry.phase_id = None

    def assigned_to_game(self, game_id: str) -> list[NpcEntry]:
        """All entries currently assigned to ``game_id``. Used by the
        game-end hook to release every bot in one pass."""
        return [e for e in self._entries.values() if e.game_id == game_id]

    # ---------------------------------------------------------- listeners

    def add_listener(self, fn: Callable[[set[str], set[str]], Awaitable[None]]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[set[str], set[str]], Awaitable[None]]) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(fn)

    def _notify_listeners_sync(self, added: set[str], removed: set[str]) -> None:
        """Schedule async notifications without blocking the registry path.

        Listeners are typically WS pushers; they may take time to write.
        We schedule them on the running loop so callers (handlers + tests)
        do not have to await listener completion.
        """
        if not self._listeners:
            return
        import asyncio as _aio

        try:
            loop = _aio.get_running_loop()
        except RuntimeError:
            # No loop — listeners cannot run. Tests that exercise the sync
            # path can still observe the entry state.
            return
        for listener in list(self._listeners):
            coro = listener(added, removed)
            task: object = loop.create_task(coro)  # type: ignore[arg-type]
            self._listener_tasks.add(task)
            task.add_done_callback(self._listener_tasks.discard)  # type: ignore[attr-defined]


__all__ = ["InMemoryNpcRegistry", "NpcEntry", "NpcRegistry"]
