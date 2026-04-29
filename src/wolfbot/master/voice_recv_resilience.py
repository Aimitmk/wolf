"""Make ``discord.ext.voice_recv``'s ``PacketRouter`` survive single-packet
opus decode failures.

Upstream's :meth:`PacketRouter._do_run` lets any exception from
``decoder.pop_data()`` propagate out of the inner ``for`` loop, which then
escapes ``run()``'s outer ``try``, logs once, sets ``reader.error``, and
calls ``voice_client.stop_listening()`` in ``finally``. After a single
``discord.opus.OpusError("corrupted stream")``, the entire RX path is
dead — Master can no longer hear humans, no STT runs, and the reactive
voice arbiter never sees a ``speech_event`` to dispatch NPCs against.

The corruption is per-packet (or at worst per-SSRC). Catching
``OpusError`` inside the inner loop keeps the thread alive and lets the
next valid packet decode normally. Sink write failures get the same
treatment so a bug in one ``sink.write`` callback never kills the
reader thread.

Apply once at startup via :func:`apply_packet_router_resilience` before
``voice_recv.VoiceRecvClient`` is connected. The patch is idempotent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord.ext.voice_recv.router import PacketRouter

log = logging.getLogger(__name__)

_PATCH_MARKER = "_wolfbot_packet_router_resilient"


def apply_packet_router_resilience() -> None:
    """Replace ``PacketRouter._do_run`` with a per-iteration try/except.

    Idempotent — calling more than once is a no-op so import-time wiring
    plus an explicit call from ``main.py`` is safe.
    """
    from discord.ext.voice_recv.router import PacketRouter

    if getattr(PacketRouter, _PATCH_MARKER, False):
        return

    def _do_run_resilient(self: PacketRouter) -> None:
        from discord.opus import OpusError

        while not self._end_thread.is_set():
            self.waiter.wait()
            with self._lock:
                for decoder in self.waiter.items:
                    ssrc = getattr(decoder, "ssrc", None)
                    try:
                        data = decoder.pop_data()
                    except OpusError as exc:
                        log.warning(
                            "voice_recv_opus_decode_skip ssrc=%s err=%s",
                            ssrc,
                            exc,
                        )
                        continue
                    except Exception:
                        log.exception(
                            "voice_recv_pop_data_failed ssrc=%s", ssrc
                        )
                        continue
                    if data is None:
                        continue
                    try:
                        self.sink.write(data.source, data)
                    except Exception:
                        log.exception(
                            "voice_recv_sink_write_failed ssrc=%s", ssrc
                        )

    PacketRouter._do_run = _do_run_resilient  # type: ignore[method-assign]
    setattr(PacketRouter, _PATCH_MARKER, True)
    log.info("voice_recv_packet_router_resilience_applied")


__all__ = ["apply_packet_router_resilience"]
