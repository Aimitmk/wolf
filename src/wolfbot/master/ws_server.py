"""Master ↔ NPC / voice-ingest WebSocket transport.

Listens on `127.0.0.1` and authenticates each connection with the shared
`MASTER_NPC_PSK` value. Two connection roles are accepted:

- ``role=npc``  — an NPC bot worker. Identified by `npc_id`. Master responds
  with ``npc_registered`` once its ``npc_register`` message arrives, and
  routes subsequent ``logic_packet`` / ``speak_request`` messages back via
  the per-connection ``send`` callback.
- ``role=voice-ingest``  — the voice ingest worker. Master pushes the current
  ``registry_snapshot`` as soon as the connection authenticates and a
  ``registry_update`` whenever the NPC bot identity set changes.

The transport is intentionally narrow: it parses incoming JSON envelopes, hands
off typed messages to a handler, and exposes a back-channel ``send`` callable
that serializes typed Pydantic messages back over the WS. Higher-level
behaviors (arbitration, authorization, audit-row writes) live in
``master_ingest_service`` / ``speak_arbiter`` and call into this module only
through the back-channel.

Testability:
- ``MasterWsServer`` is a Protocol; production code uses
  ``WebsocketsMasterWsServer`` (real `websockets` library) and tests use
  ``FakeMasterWsServer`` from ``tests.fakes``.
- The server exposes a `dispatch(raw_json, ctx)` entry point that the Fake
  also calls, so unit tests can simulate inbound messages without standing
  up a real socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from wolfbot.domain.ws_messages import (
    Heartbeat,
    NightActionDecision,
    NpcRegister,
    NpcRegistered,
    PlaybackFailed,
    PlaybackFinished,
    RegistrySnapshot,
    RegistryUpdate,
    SpeakResult,
    SpeechEventPayload,
    SttFailed,
    TtsFailed,
    TtsFinished,
    VadSpeechEnded,
    VadSpeechStarted,
    VoteDecision,
)
from wolfbot.master.npc_registry import NpcRegistry

log = logging.getLogger(__name__)


ConnectionRole = Literal["npc", "voice-ingest"]


@dataclass
class ConnectionContext:
    """Per-connection state passed to message handlers.

    Holds the connection role, an arbitrary tag (npc_id or "voice-ingest"),
    and the back-channel `send` coroutine so handlers can push typed
    responses without depending on the underlying WS library.
    """

    role: ConnectionRole
    tag: str
    send: Callable[[str], Awaitable[None]]
    closed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# Handler shape: takes (parsed_json: dict, ctx: ConnectionContext) and returns
# nothing. The dispatcher selects the right handler from the `type` field.
MessageHandler = Callable[[dict[str, Any], ConnectionContext], Awaitable[None]]


@runtime_checkable
class MasterWsServer(Protocol):
    """Operational surface used by the orchestrator (`main.py` / tests)."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def add_handler(self, message_type: str, handler: MessageHandler) -> None: ...

    async def broadcast_to_voice_ingest(self, message_json: str) -> None: ...


class HandlerRegistry:
    """Routes message types to their async handlers.

    Kept as a tiny independent class so ``WebsocketsMasterWsServer`` and
    ``FakeMasterWsServer`` can both delegate to the same dispatch path.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, MessageHandler] = {}

    def add(self, message_type: str, handler: MessageHandler) -> None:
        self._handlers[message_type] = handler

    async def dispatch(self, raw_json: str, ctx: ConnectionContext) -> None:
        try:
            payload: Any = json.loads(raw_json)
        except json.JSONDecodeError:
            log.warning("master_ws_invalid_json role=%s tag=%s", ctx.role, ctx.tag)
            return
        if not isinstance(payload, dict) or "type" not in payload:
            log.warning(
                "master_ws_missing_type role=%s tag=%s payload=%s",
                ctx.role,
                ctx.tag,
                payload,
            )
            return
        message_type = payload["type"]
        handler = self._handlers.get(message_type)
        if handler is None:
            log.info(
                "master_ws_unhandled_message type=%s role=%s tag=%s",
                message_type,
                ctx.role,
                ctx.tag,
            )
            return
        try:
            await handler(payload, ctx)
        except Exception:
            log.exception(
                "master_ws_handler_failed type=%s role=%s tag=%s",
                message_type,
                ctx.role,
                ctx.tag,
            )


# ---------------------------------------------------------------- handlers


def _now_ms_default() -> int:
    import time

    return int(time.time() * 1000)


@dataclass
class MasterHandlers:
    """The collection of typed handlers Master needs.

    Each handler reads from / writes to a small set of collaborators:
    ``NpcRegistry`` (presence), the ``ingest_service`` for voice-ingest
    messages, and the ``arbiter`` (set later in the apply flow) for
    speak-result handling.

    Keeping the handlers in a dataclass lets ``main.py`` wire them once and
    ``tests/test_master_ws_server.py`` substitute Fakes per call.
    """

    registry: NpcRegistry
    on_speak_result: Callable[[SpeakResult, ConnectionContext], Awaitable[None]] | None = None
    on_tts_finished: Callable[[TtsFinished, ConnectionContext], Awaitable[None]] | None = None
    on_tts_failed: Callable[[TtsFailed, ConnectionContext], Awaitable[None]] | None = None
    on_playback_finished: (
        Callable[[PlaybackFinished, ConnectionContext], Awaitable[None]] | None
    ) = None
    on_playback_failed: Callable[[PlaybackFailed, ConnectionContext], Awaitable[None]] | None = None
    on_speech_event_payload: (
        Callable[[SpeechEventPayload, ConnectionContext], Awaitable[None]] | None
    ) = None
    on_vad_started: Callable[[VadSpeechStarted, ConnectionContext], Awaitable[None]] | None = None
    on_vad_ended: Callable[[VadSpeechEnded, ConnectionContext], Awaitable[None]] | None = None
    on_stt_failed: Callable[[SttFailed, ConnectionContext], Awaitable[None]] | None = None
    on_vote_decision: (
        Callable[[VoteDecision, ConnectionContext], Awaitable[None]] | None
    ) = None
    on_night_action_decision: (
        Callable[[NightActionDecision, ConnectionContext], Awaitable[None]] | None
    ) = None
    now_ms: Callable[[], int] = field(default=_now_ms_default)

    def install(self, registry_: HandlerRegistry) -> None:
        registry_.add("npc_register", self._handle_register)
        registry_.add("heartbeat", self._handle_heartbeat)
        registry_.add("speak_result", self._handle_speak_result)
        registry_.add("tts_finished", self._handle_tts_finished)
        registry_.add("tts_failed", self._handle_tts_failed)
        registry_.add("playback_finished", self._handle_playback_finished)
        registry_.add("playback_failed", self._handle_playback_failed)
        registry_.add("speech_event_payload", self._handle_speech_payload)
        registry_.add("vad_speech_started", self._handle_vad_started)
        registry_.add("vad_speech_ended", self._handle_vad_ended)
        registry_.add("stt_failed", self._handle_stt_failed)
        registry_.add("vote_decision", self._handle_vote_decision)
        registry_.add("night_action_decision", self._handle_night_action_decision)

    async def _handle_register(self, payload: dict[str, Any], ctx: ConnectionContext) -> None:
        msg = NpcRegister.model_validate(payload)
        ctx.tag = msg.npc_id
        entry = self.registry.register(
            npc_id=msg.npc_id,
            discord_bot_user_id=msg.discord_bot_user_id,
            persona_key=msg.persona_key,
            supported_voices=msg.supported_voices,
            version=msg.version,
            send=ctx.send,
            now_ms=self.now_ms(),
        )
        reply = NpcRegistered(
            ts=self.now_ms(),
            trace_id=msg.trace_id,
            npc_id=msg.npc_id,
            assigned_seat=entry.assigned_seat,
            game_id=entry.game_id,
            phase_id=entry.phase_id,
        )
        await ctx.send(reply.model_dump_json())

    async def _handle_heartbeat(self, payload: dict[str, Any], ctx: ConnectionContext) -> None:
        msg = Heartbeat.model_validate(payload)
        if msg.npc_id is not None:
            self.registry.heartbeat(msg.npc_id, self.now_ms())

    async def _handle_speak_result(self, payload: dict[str, Any], ctx: ConnectionContext) -> None:
        msg = SpeakResult.model_validate(payload)
        if self.on_speak_result is not None:
            await self.on_speak_result(msg, ctx)

    async def _handle_tts_finished(self, payload: dict[str, Any], ctx: ConnectionContext) -> None:
        msg = TtsFinished.model_validate(payload)
        if self.on_tts_finished is not None:
            await self.on_tts_finished(msg, ctx)

    async def _handle_tts_failed(self, payload: dict[str, Any], ctx: ConnectionContext) -> None:
        msg = TtsFailed.model_validate(payload)
        if self.on_tts_failed is not None:
            await self.on_tts_failed(msg, ctx)

    async def _handle_playback_finished(
        self, payload: dict[str, Any], ctx: ConnectionContext
    ) -> None:
        msg = PlaybackFinished.model_validate(payload)
        if self.on_playback_finished is not None:
            await self.on_playback_finished(msg, ctx)

    async def _handle_playback_failed(
        self, payload: dict[str, Any], ctx: ConnectionContext
    ) -> None:
        msg = PlaybackFailed.model_validate(payload)
        if self.on_playback_failed is not None:
            await self.on_playback_failed(msg, ctx)

    async def _handle_speech_payload(self, payload: dict[str, Any], ctx: ConnectionContext) -> None:
        msg = SpeechEventPayload.model_validate(payload)
        if self.on_speech_event_payload is not None:
            await self.on_speech_event_payload(msg, ctx)

    async def _handle_vad_started(self, payload: dict[str, Any], ctx: ConnectionContext) -> None:
        msg = VadSpeechStarted.model_validate(payload)
        if self.on_vad_started is not None:
            await self.on_vad_started(msg, ctx)

    async def _handle_vad_ended(self, payload: dict[str, Any], ctx: ConnectionContext) -> None:
        msg = VadSpeechEnded.model_validate(payload)
        if self.on_vad_ended is not None:
            await self.on_vad_ended(msg, ctx)

    async def _handle_stt_failed(self, payload: dict[str, Any], ctx: ConnectionContext) -> None:
        msg = SttFailed.model_validate(payload)
        if self.on_stt_failed is not None:
            await self.on_stt_failed(msg, ctx)

    async def _handle_vote_decision(
        self, payload: dict[str, Any], ctx: ConnectionContext
    ) -> None:
        msg = VoteDecision.model_validate(payload)
        if self.on_vote_decision is not None:
            await self.on_vote_decision(msg, ctx)

    async def _handle_night_action_decision(
        self, payload: dict[str, Any], ctx: ConnectionContext
    ) -> None:
        msg = NightActionDecision.model_validate(payload)
        if self.on_night_action_decision is not None:
            await self.on_night_action_decision(msg, ctx)


# ---------------------------------------------------------------- real WS server


class WebsocketsMasterWsServer:
    """Production WebSocket server.

    Loads the `websockets` library lazily so tests do not need it. The
    handshake reads two query parameters from the WebSocket URL: ``role``
    (``npc`` | ``voice-ingest``) and ``psk``. Mismatch on either rejects the
    connection with a typed ``HandshakeError`` written before close. Behaviour
    of the protocol after handshake is delegated to ``HandlerRegistry``.

    The server is intentionally minimal — only the connection lifecycle plus
    a back-channel push API for voice-ingest registry updates. Higher-level
    state (NPC presence, audit rows) lives in ``NpcRegistry`` /
    ``master_ingest_service`` / ``speak_arbiter``.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8800,
        psk: str,
        registry: NpcRegistry,
        handlers: MasterHandlers,
    ) -> None:
        self.host = host
        self.port = port
        self.psk = psk
        self.registry = registry
        self.handler_registry = HandlerRegistry()
        handlers.install(self.handler_registry)
        self._handlers = handlers
        self._server: Any = None
        self._voice_ingest_conns: set[ConnectionContext] = set()
        # Subscribe to registry deltas so we can push registry_update to all
        # voice-ingest peers in real time.
        add_listener = getattr(self.registry, "add_listener", None)
        if add_listener is not None:
            add_listener(self._on_registry_delta)

    async def start(self) -> None:
        # Lazy import — the live websockets library is a heavy dep we only
        # exercise when actually serving production traffic. Tests use the
        # Fake and never call start().
        import websockets

        async def _conn_handler(ws: Any) -> None:
            ctx = await self._authenticate(ws)
            if ctx is None:
                return
            try:
                if ctx.role == "voice-ingest":
                    self._voice_ingest_conns.add(ctx)
                    await self._send_initial_registry_snapshot(ctx)
                async for raw in ws:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    await self.handler_registry.dispatch(raw, ctx)
            finally:
                ctx.closed = True
                self._voice_ingest_conns.discard(ctx)
                if ctx.role == "npc":
                    self.registry.unregister(ctx.tag, reason="ws_closed")

        self._server = await websockets.serve(_conn_handler, self.host, self.port)
        log.info(
            "master_ws_listening host=%s port=%d psk_set=%s",
            self.host,
            self.port,
            bool(self.psk),
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _authenticate(self, ws: Any) -> ConnectionContext | None:
        from urllib.parse import parse_qs, urlparse

        # websockets ≥13 (including 16.0) exposes the HTTP request as
        # `ws.request` (a `Request` with `.path`).  Older versions exposed
        # `ws.path` directly.  Try the modern attribute first.
        request = getattr(ws, "request", None)
        if request is not None:
            path = getattr(request, "path", None) or ""
        else:
            path = getattr(ws, "path", "") or ""
        query = parse_qs(urlparse(path).query)
        role_raw = (query.get("role") or [""])[0]
        psk_raw = (query.get("psk") or [""])[0]
        role: ConnectionRole | None = None
        if role_raw == "npc":
            role = "npc"
        elif role_raw == "voice-ingest":
            role = "voice-ingest"
        if role is None or psk_raw != self.psk:
            log.warning(
                "master_ws_auth_rejected role=%s psk_match=%s",
                role_raw,
                psk_raw == self.psk,
            )
            with contextlib.suppress(Exception):
                await ws.close(code=4401, reason="auth_failed")
            return None

        async def _send(msg: str) -> None:
            try:
                await ws.send(msg)
            except Exception:
                log.exception("master_ws_send_failed role=%s", role)

        return ConnectionContext(role=role, tag=role_raw, send=_send)

    def add_handler(self, message_type: str, handler: MessageHandler) -> None:
        self.handler_registry.add(message_type, handler)

    async def broadcast_to_voice_ingest(self, message_json: str) -> None:
        for ctx in list(self._voice_ingest_conns):
            if ctx.closed:
                self._voice_ingest_conns.discard(ctx)
                continue
            try:
                await ctx.send(message_json)
            except Exception:
                log.exception("voice_ingest_broadcast_failed tag=%s", ctx.tag)

    async def _on_registry_delta(self, added: set[str], removed: set[str]) -> None:
        if not added and not removed:
            return
        update = RegistryUpdate(
            ts=self._handlers.now_ms(),
            trace_id="registry-update",
            added=tuple(sorted(added)),
            removed=tuple(sorted(removed)),
        )
        await self.broadcast_to_voice_ingest(update.model_dump_json())

    async def _send_initial_registry_snapshot(self, ctx: ConnectionContext) -> None:
        snapshot = RegistrySnapshot(
            ts=self._handlers.now_ms(),
            trace_id="registry-snapshot",
            npc_user_ids=tuple(sorted(self.registry.discord_bot_user_ids())),
        )
        await ctx.send(snapshot.model_dump_json())


__all__ = [
    "ConnectionContext",
    "ConnectionRole",
    "HandlerRegistry",
    "MasterHandlers",
    "MasterWsServer",
    "MessageHandler",
    "WebsocketsMasterWsServer",
]


# Force a non-static reference so unused-import linters keep `asyncio` here for
# implementations that subclass / extend this module without re-importing.
_ = asyncio
