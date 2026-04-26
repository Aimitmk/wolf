"""voice-ingest → Master WebSocket client.

Sends `vad_speech_started` / `vad_speech_ended` / `speech_event_payload`
/ `stt_failed` / `heartbeat` to Master and consumes `registry_snapshot` /
`registry_update`.

The transport is a `websockets` client connection; tests substitute
`FakeMasterIngestionClient`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from wolfbot.domain.ws_messages import (
    Heartbeat,
    SpeechEventPayload,
    SttFailed,
    VadSpeechEnded,
    VadSpeechStarted,
)

log = logging.getLogger(__name__)


@runtime_checkable
class MasterIngestionClient(Protocol):
    """The voice-ingest worker's outbound API to Master."""

    async def send_vad_started(self, msg: VadSpeechStarted) -> None: ...
    async def send_vad_ended(self, msg: VadSpeechEnded) -> None: ...
    async def send_speech_event_payload(self, msg: SpeechEventPayload) -> None: ...
    async def send_stt_failed(self, msg: SttFailed) -> None: ...
    async def send_heartbeat(self, msg: Heartbeat) -> None: ...


@runtime_checkable
class NpcRegistryView(Protocol):
    """Read-side view kept locally on the voice-ingest worker.

    Updated by `apply_snapshot` / `apply_update` triggered by Master pushes.
    """

    def is_npc(self, discord_user_id: str) -> bool: ...

    def npc_user_ids(self) -> set[str]: ...


class InMemoryNpcRegistryView:
    """The default `NpcRegistryView` used by the voice-ingest worker.

    Fail-closed: if no snapshot has arrived yet, the set is empty and all
    audio is processed (the Master-side `npc_stt_discarded` guard prevents
    any STT->SpeechEvent leakage during this window — see voice-ingest
    spec).
    """

    def __init__(self) -> None:
        self._ids: set[str] = set()

    def apply_snapshot(self, npc_user_ids: tuple[str, ...]) -> None:
        self._ids = set(npc_user_ids)

    def apply_update(self, added: tuple[str, ...], removed: tuple[str, ...]) -> None:
        for uid in removed:
            self._ids.discard(uid)
        for uid in added:
            self._ids.add(uid)

    def is_npc(self, discord_user_id: str) -> bool:
        return discord_user_id in self._ids

    def npc_user_ids(self) -> set[str]:
        return set(self._ids)


class FakeMasterIngestionClient:
    """Captures every outbound message in-memory for assertion-based tests."""

    def __init__(self) -> None:
        self.vad_started: list[VadSpeechStarted] = []
        self.vad_ended: list[VadSpeechEnded] = []
        self.speech_payloads: list[SpeechEventPayload] = []
        self.stt_failures: list[SttFailed] = []
        self.heartbeats: list[Heartbeat] = []

    async def send_vad_started(self, msg: VadSpeechStarted) -> None:
        self.vad_started.append(msg)

    async def send_vad_ended(self, msg: VadSpeechEnded) -> None:
        self.vad_ended.append(msg)

    async def send_speech_event_payload(self, msg: SpeechEventPayload) -> None:
        self.speech_payloads.append(msg)

    async def send_stt_failed(self, msg: SttFailed) -> None:
        self.stt_failures.append(msg)

    async def send_heartbeat(self, msg: Heartbeat) -> None:
        self.heartbeats.append(msg)


class WebsocketsMasterIngestionClient:
    """Production client using the `websockets` library.

    Connects to Master's localhost endpoint with `role=voice-ingest&psk=...`
    and serializes outbound messages as JSON. Inbound `registry_snapshot`
    / `registry_update` events are dispatched to user-supplied callbacks.
    """

    def __init__(
        self,
        *,
        url: str,
        psk: str,
        on_registry_snapshot: Callable[[tuple[str, ...]], None],
        on_registry_update: Callable[[tuple[str, ...], tuple[str, ...]], None],
    ) -> None:
        self.url = url
        self.psk = psk
        self._on_snapshot = on_registry_snapshot
        self._on_update = on_registry_update
        self._ws: object | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        import websockets

        sep = "?" if "?" not in self.url else "&"
        full = f"{self.url}{sep}role=voice-ingest&psk={self.psk}"
        self._ws = await websockets.connect(full)
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws is not None:
            close = getattr(self._ws, "close", None)
            if close is not None:
                await close()
            self._ws = None

    async def _reader_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        async for raw in ws:  # type: ignore[attr-defined]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = payload.get("type")
            if t == "registry_snapshot":
                self._on_snapshot(tuple(payload.get("npc_user_ids", ())))
            elif t == "registry_update":
                self._on_update(
                    tuple(payload.get("added", ())),
                    tuple(payload.get("removed", ())),
                )

    async def _send(self, message_json: str) -> None:
        ws = self._ws
        if ws is None:
            return
        async with self._lock:
            try:
                await ws.send(message_json)  # type: ignore[attr-defined]
            except Exception:
                log.exception("voice_ingest_send_failed")

    async def send_vad_started(self, msg: VadSpeechStarted) -> None:
        await self._send(msg.model_dump_json())

    async def send_vad_ended(self, msg: VadSpeechEnded) -> None:
        await self._send(msg.model_dump_json())

    async def send_speech_event_payload(self, msg: SpeechEventPayload) -> None:
        await self._send(msg.model_dump_json())

    async def send_stt_failed(self, msg: SttFailed) -> None:
        await self._send(msg.model_dump_json())

    async def send_heartbeat(self, msg: Heartbeat) -> None:
        await self._send(msg.model_dump_json())


# Listener helper used by the registry view during reconnects: ensure the
# view applies a snapshot once and caches deltas thereafter. The functional
# wiring lives in the orchestrator so we keep this module dependency-light.

ListenerFactory = Callable[
    [InMemoryNpcRegistryView],
    tuple[
        Callable[[tuple[str, ...]], None],
        Callable[[tuple[str, ...], tuple[str, ...]], None],
    ],
]


def make_default_listeners(
    view: InMemoryNpcRegistryView,
) -> tuple[
    Callable[[tuple[str, ...]], None],
    Callable[[tuple[str, ...], tuple[str, ...]], None],
]:
    def on_snapshot(npc_user_ids: tuple[str, ...]) -> None:
        view.apply_snapshot(npc_user_ids)

    def on_update(added: tuple[str, ...], removed: tuple[str, ...]) -> None:
        view.apply_update(added, removed)

    return on_snapshot, on_update


__all__ = [
    "FakeMasterIngestionClient",
    "InMemoryNpcRegistryView",
    "ListenerFactory",
    "MasterIngestionClient",
    "NpcRegistryView",
    "WebsocketsMasterIngestionClient",
    "make_default_listeners",
]


# Re-bind Awaitable so type checkers keep the import.
_ = Awaitable
