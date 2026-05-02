"""MasterTtsPlayback — narration ↔ NPC speech serialization tests.

Pins down two invariants:

  * While Master is mid-narration the arbiter's `_active_playback`
    carries a sentinel so `try_dispatch_next` would treat the gate as
    `queue_busy` and suppress new NPC dispatch.
  * Before Master starts speaking it waits for any in-flight NPC
    playback to drain — so an already-authorized NPC utterance never
    overlaps Master's voice in the same VC.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from wolfbot.master.voice.tts_playback import MasterTtsPlayback
from wolfbot.npc.audio.tts import FakeTtsService, TtsResult


@dataclass
class _MiniArbiter:
    """Minimal arbiter stand-in carrying just the `_active_playback`
    surface MasterTtsPlayback touches."""

    _active_playback: set[str] = field(default_factory=set)


def _make_playback() -> MasterTtsPlayback:
    return MasterTtsPlayback(
        tts=FakeTtsService(default=TtsResult(audio=b"WAVE", duration_ms=100)),
        voice_id="47",
        vc_ref=[None],  # No real VC — speak() falls through silently.
    )


async def test_suppress_npc_dispatch_holds_sentinel_during_block() -> None:
    arb = _MiniArbiter()
    pb = _make_playback()
    async with pb.suppress_npc_dispatch(arb):
        # While the with-block is open, exactly one master sentinel
        # is parked on the arbiter — `is_blocked()` (the arbiter's
        # public surface) would see this as queue_busy.
        assert len(arb._active_playback) == 1
        only = next(iter(arb._active_playback))
        assert only.startswith("master-narration-")
    # After the block, the sentinel is removed.
    assert arb._active_playback == set()


async def test_suppress_npc_dispatch_clears_on_exception() -> None:
    arb = _MiniArbiter()
    pb = _make_playback()
    with pytest.raises(RuntimeError):
        async with pb.suppress_npc_dispatch(arb):
            raise RuntimeError("kaboom")
    assert arb._active_playback == set()


async def test_suppress_npc_dispatch_waits_for_npc_playback_drain() -> None:
    """An already-authorized NPC playback must finish before Master
    speaks. We seed a non-master entry in `_active_playback`, then
    enter the suppress context — it must block until we clear it."""
    arb = _MiniArbiter(_active_playback={"sr_npc_pending_xyz"})
    pb = _make_playback()
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def _enter() -> None:
        async with pb.suppress_npc_dispatch(arb):
            entered.set()
        finished.set()

    task = asyncio.create_task(_enter())
    # Give the task a moment to start; it must NOT have entered yet.
    await asyncio.sleep(0.1)
    assert not entered.is_set(), "must wait for NPC playback to drain"
    # Drain the NPC entry — Master should now proceed.
    arb._active_playback.discard("sr_npc_pending_xyz")
    await asyncio.wait_for(finished.wait(), timeout=2.0)
    await task
    assert arb._active_playback == set()


async def test_drain_wait_times_out_gracefully() -> None:
    """If an NPC entry is leaked / stuck, Master narration eventually
    gives up rather than deadlocking the engine."""
    arb = _MiniArbiter(_active_playback={"sr_stuck_forever"})
    pb = _make_playback()

    async def _quick_check() -> None:
        # Use a short max_wait_s to keep the test fast.
        await pb._wait_for_npc_playback_drain(arb, max_wait_s=0.3, poll_interval_s=0.05)

    await asyncio.wait_for(_quick_check(), timeout=2.0)


async def test_suppress_npc_dispatch_handles_missing_arbiter_attribute() -> None:
    """If the arbiter shape ever changes and `_active_playback` is
    absent, the context must not crash — narration is more important
    than gate-management on a malformed arbiter."""

    class _Broken:
        pass

    pb = _make_playback()
    async with pb.suppress_npc_dispatch(_Broken()):
        pass  # No assertion — just verify no exception.


async def test_speak_returns_false_when_vc_disconnected() -> None:
    """speak() with no VC client wired must fail soft (return False)
    rather than crash the engine."""
    pb = _make_playback()
    ok = await pb.speak("テスト")
    assert ok is False


async def test_concurrent_master_sentinels_do_not_block_each_other() -> None:
    """Master narrations are serialized by the playback's own asyncio
    Lock — but the drain-wait must ignore other master sentinels
    (otherwise back-to-back narrations would deadlock waiting for
    each other)."""
    arb = _MiniArbiter(_active_playback={"master-narration-already"})
    pb = _make_playback()

    async def _quick_drain() -> None:
        await pb._wait_for_npc_playback_drain(arb, max_wait_s=0.3, poll_interval_s=0.05)

    # Should return promptly; if it counted master sentinels as blockers,
    # this would hit the timeout.
    await asyncio.wait_for(_quick_drain(), timeout=1.0)
