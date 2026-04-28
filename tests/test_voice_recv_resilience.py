"""Tests for the ``PacketRouter`` resilience patch.

The upstream :mod:`discord.ext.voice_recv` thread dies on the first
``OpusError("corrupted stream")``, taking down the entire RX path —
seen in production on 2026-04-28 right after Master joined VC and
started narration. After the patch, a single bad packet must drop the
frame and the thread must keep dispatching subsequent valid frames so
STT and the reactive_voice arbiter keep working.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import discord.opus
import pytest
from discord.ext.voice_recv.router import PacketRouter

# Resolve libopus before constructing ``OpusError(-4)`` — outside of an
# actual VC connection the C library isn't loaded by default and the
# constructor calls ``_lib.opus_strerror(...)`` which would raise
# ``AttributeError``. ``_load_default()`` is the same routine the SDK
# runs lazily when a real VC connection negotiates encryption.
discord.opus._load_default()

from discord.opus import OpusError  # noqa: E402

from wolfbot.master.voice_recv_resilience import (  # noqa: E402
    apply_packet_router_resilience,
)


def _make_decoder(ssrc: int, behavior: list[Any]) -> SimpleNamespace:
    """Decoder stub whose ``pop_data`` cycles through ``behavior``.

    Each entry is either an ``Exception`` instance (to raise) or a value
    to return from ``pop_data``. ``None`` entries simulate an empty pop.
    """
    iterator = iter(behavior)

    def pop_data() -> Any:
        nxt = next(iterator)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    return SimpleNamespace(ssrc=ssrc, pop_data=pop_data)


class _FakeWaiter:
    """``MultiDataEvent`` stand-in: hand a single batch then notify done."""

    def __init__(self, items: list[Any], done: threading.Event) -> None:
        self.items = items
        self._done = done
        self._delivered = False

    def wait(self) -> None:
        if self._delivered:
            self._done.wait(timeout=2.0)
            return
        self._delivered = True


def _build_router(items: list[Any]) -> tuple[PacketRouter, list, threading.Event]:
    """Construct a router stub wired with our fake waiter and capture sink."""
    end = threading.Event()
    captured: list[tuple[Any, Any]] = []

    sink = SimpleNamespace(write=lambda src, data: captured.append((src, data)))
    router = PacketRouter.__new__(PacketRouter)
    threading.Thread.__init__(router, daemon=True)
    router.sink = sink  # type: ignore[attr-defined]
    router._lock = threading.RLock()  # type: ignore[attr-defined]
    router._end_thread = end  # type: ignore[attr-defined]
    router.waiter = _FakeWaiter(items, end)  # type: ignore[attr-defined]
    return router, captured, end


def test_apply_is_idempotent() -> None:
    apply_packet_router_resilience()
    first = PacketRouter._do_run
    apply_packet_router_resilience()
    assert PacketRouter._do_run is first


def test_opus_error_does_not_kill_thread_subsequent_frames_dispatched() -> None:
    """One bad pop_data → that decoder skipped that round; the next valid
    decoder in the same batch still reaches sink.write."""
    apply_packet_router_resilience()

    valid_data = SimpleNamespace(source="user_b", payload="ok")
    bad = _make_decoder(11, [OpusError(-4)])
    good = _make_decoder(22, [valid_data])

    router, captured, end = _build_router([bad, good])
    # Run one iteration synchronously then flip the end flag.
    t = threading.Thread(target=router._do_run, daemon=True)
    t.start()
    # Give the loop one tick, then stop it.
    time.sleep(0.05)
    end.set()
    t.join(timeout=1.0)

    assert not t.is_alive(), "router thread died despite resilient patch"
    assert captured == [("user_b", valid_data)]


def test_sink_write_failure_does_not_kill_thread() -> None:
    """A buggy sink.write must not propagate — the next decoder still gets
    serviced and the thread stays up."""
    apply_packet_router_resilience()

    end = threading.Event()
    written: list[Any] = []

    def writer(src: Any, data: Any) -> None:
        if data.payload == "explode":
            raise RuntimeError("sink boom")
        written.append((src, data))

    sink = SimpleNamespace(write=writer)
    bad_data = SimpleNamespace(source="x", payload="explode")
    good_data = SimpleNamespace(source="y", payload="ok")

    bad = _make_decoder(1, [bad_data])
    good = _make_decoder(2, [good_data])

    router = PacketRouter.__new__(PacketRouter)
    threading.Thread.__init__(router, daemon=True)
    router.sink = sink  # type: ignore[attr-defined]
    router._lock = threading.RLock()  # type: ignore[attr-defined]
    router._end_thread = end  # type: ignore[attr-defined]
    router.waiter = _FakeWaiter([bad, good], end)  # type: ignore[attr-defined]

    t = threading.Thread(target=router._do_run, daemon=True)
    t.start()
    time.sleep(0.05)
    end.set()
    t.join(timeout=1.0)

    assert not t.is_alive()
    assert written == [("y", good_data)]


def test_none_pop_data_is_ignored_without_calling_sink() -> None:
    """``pop_data`` returning ``None`` is the normal "no completed frame"
    case — must not be forwarded to the sink."""
    apply_packet_router_resilience()

    end = threading.Event()
    captured: list[Any] = []
    sink = SimpleNamespace(write=lambda *a, **kw: captured.append(a))

    empty = _make_decoder(7, [None])
    router = PacketRouter.__new__(PacketRouter)
    threading.Thread.__init__(router, daemon=True)
    router.sink = sink  # type: ignore[attr-defined]
    router._lock = threading.RLock()  # type: ignore[attr-defined]
    router._end_thread = end  # type: ignore[attr-defined]
    router.waiter = _FakeWaiter([empty], end)  # type: ignore[attr-defined]

    t = threading.Thread(target=router._do_run, daemon=True)
    t.start()
    time.sleep(0.05)
    end.set()
    t.join(timeout=1.0)

    assert not t.is_alive()
    assert captured == []


@pytest.mark.parametrize("exc", [ValueError("oops"), KeyError("k"), OpusError(-1)])
def test_arbitrary_pop_data_exception_is_swallowed(exc: BaseException) -> None:
    """OpusError is the known failure mode; any other unexpected exception
    must also be contained so the thread keeps draining packets."""
    apply_packet_router_resilience()

    end = threading.Event()
    captured: list[Any] = []
    sink = SimpleNamespace(write=lambda src, data: captured.append((src, data)))

    bad = _make_decoder(99, [exc])
    good = _make_decoder(100, [SimpleNamespace(source="u", payload="ok")])

    router = PacketRouter.__new__(PacketRouter)
    threading.Thread.__init__(router, daemon=True)
    router.sink = sink  # type: ignore[attr-defined]
    router._lock = threading.RLock()  # type: ignore[attr-defined]
    router._end_thread = end  # type: ignore[attr-defined]
    router.waiter = _FakeWaiter([bad, good], end)  # type: ignore[attr-defined]

    t = threading.Thread(target=router._do_run, daemon=True)
    t.start()
    time.sleep(0.05)
    end.set()
    t.join(timeout=1.0)

    assert not t.is_alive()
    assert len(captured) == 1
