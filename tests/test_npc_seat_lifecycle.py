"""NPC bot seat lifecycle — verify the assignment / release plumbing.

Goal: NPC bots no longer auto-join VC at startup. They idle (Discord +
WS connected) until Master picks them via `/wolf start` and sends a
`seat_assigned`; at game end Master sends `seat_released` and the bot
leaves VC. This file pins down each piece of that contract.

Covers:
- `NpcClient` invokes its `on_vc_join` callback when Master sends
  `seat_assigned`, and `on_vc_leave` when `seat_released` arrives.
- A reconnecting NPC whose registration reply already names a seat
  still triggers `on_vc_join` (recovery path).
- `npc_registered` with `assigned_seat=None` does NOT trigger join.
- `NpcRegistry.unassign` clears the entry's assignment fields.
- `NpcRegistry.assigned_to_game` returns only entries currently
  assigned to the given game id (used by Master's release sweep).
- `seat_assigned` / `seat_released` round-trip through the
  Pydantic envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wolfbot.domain.ws_messages import (
    NpcRegistered,
    SeatAssigned,
    SeatReleased,
    SetMuteState,
)
from wolfbot.master.ws.npc_registry import InMemoryNpcRegistry
from wolfbot.npc.audio.playback import FakeVoicePlayback
from wolfbot.npc.audio.tts import FakeTtsService
from wolfbot.npc.runtime.client import NpcClient, NpcClientConfig
from wolfbot.npc.speech.speech_service import FakeNpcGenerator, NpcSpeechService


@dataclass
class _VcCallbackProbe:
    join_calls: int = 0
    leave_calls: int = 0
    join_log: list[str] = field(default_factory=list)
    leave_log: list[str] = field(default_factory=list)

    async def on_join(self) -> None:
        self.join_calls += 1
        self.join_log.append("join")

    async def on_leave(self) -> None:
        self.leave_calls += 1
        self.leave_log.append("leave")


def _make_client(probe: _VcCallbackProbe) -> NpcClient:
    captured: list[str] = []

    async def send(msg: str) -> None:
        captured.append(msg)

    return NpcClient(
        config=NpcClientConfig(
            npc_id="npc_setsu",
            discord_bot_user_id="bot2",
            persona_key="setsu",
            voice_id="vox-1",
        ),
        speech=NpcSpeechService(FakeNpcGenerator()),
        tts=FakeTtsService(),
        playback=FakeVoicePlayback(),
        send=send,
        now_ms=lambda: 1,
        on_vc_join=probe.on_join,
        on_vc_leave=probe.on_leave,
    )


# ------------------------------------------------------ NpcClient lifecycle


async def test_seat_assigned_triggers_vc_join() -> None:
    probe = _VcCallbackProbe()
    client = _make_client(probe)
    msg = SeatAssigned(
        ts=1,
        trace_id="t",
        npc_id="npc_setsu",
        seat_no=2,
        game_id="g1",
        phase_id="g1::day1::DAY_DISCUSSION::1",
    )
    await client.process_message(msg.model_dump_json())
    assert probe.join_calls == 1
    assert probe.leave_calls == 0
    assert client.assigned_seat == 2
    assert client.assigned_game_id == "g1"


async def test_seat_released_triggers_vc_leave_and_clears_state() -> None:
    probe = _VcCallbackProbe()
    client = _make_client(probe)
    # Pre-assign so leave has something to clear.
    await client.process_message(
        SeatAssigned(
            ts=1, trace_id="t", npc_id="npc_setsu", seat_no=2,
            game_id="g1", phase_id="g1::day1::DAY_DISCUSSION::1",
        ).model_dump_json()
    )
    assert client.assigned_seat == 2

    await client.process_message(
        SeatReleased(
            ts=2, trace_id="t", npc_id="npc_setsu", game_id="g1",
            reason="game_ended",
        ).model_dump_json()
    )
    assert probe.leave_calls == 1
    assert client.assigned_seat is None
    assert client.assigned_game_id is None
    assert client.assigned_phase_id is None


async def test_npc_registered_without_seat_does_not_join_vc() -> None:
    """Default startup case: NPC bot registers, Master replies with no seat.
    The bot must NOT auto-join VC — it idles until /wolf start picks it."""
    probe = _VcCallbackProbe()
    client = _make_client(probe)
    msg = NpcRegistered(ts=1, trace_id="t", npc_id="npc_setsu")
    await client.process_message(msg.model_dump_json())
    assert probe.join_calls == 0
    assert probe.leave_calls == 0
    assert client.registered is True
    assert client.assigned_seat is None


async def test_npc_registered_with_assigned_seat_triggers_recovery_join() -> None:
    """Reconnect mid-game: Master tells the bot it's already assigned, and
    the bot rejoins VC without waiting for a fresh seat_assigned."""
    probe = _VcCallbackProbe()
    client = _make_client(probe)
    msg = NpcRegistered(
        ts=1,
        trace_id="t",
        npc_id="npc_setsu",
        assigned_seat=3,
        game_id="g1",
        phase_id="g1::day1::DAY_DISCUSSION::1",
    )
    await client.process_message(msg.model_dump_json())
    assert probe.join_calls == 1
    assert client.assigned_seat == 3


async def test_set_mute_state_invokes_on_set_mute_for_self() -> None:
    """`set_mute_state` flips the bot's voice self-mute via the
    `on_set_mute` callback. Mismatched npc_id is ignored."""
    captured: list[bool] = []

    async def on_set_mute(self_mute: bool) -> None:
        captured.append(self_mute)

    probe = _VcCallbackProbe()
    client = _make_client(probe)
    client.on_set_mute = on_set_mute

    await client.process_message(
        SetMuteState(
            ts=1, trace_id="t", npc_id="npc_setsu", self_mute=True
        ).model_dump_json()
    )
    await client.process_message(
        SetMuteState(
            ts=2, trace_id="t", npc_id="npc_setsu", self_mute=False
        ).model_dump_json()
    )
    # Mismatched npc_id — must be a no-op.
    await client.process_message(
        SetMuteState(
            ts=3, trace_id="t", npc_id="someone_else", self_mute=True
        ).model_dump_json()
    )
    assert captured == [True, False]


async def test_set_mute_state_callback_failure_does_not_propagate() -> None:
    async def boom(_self_mute: bool) -> None:
        raise RuntimeError("voice gateway unavailable")

    probe = _VcCallbackProbe()
    client = _make_client(probe)
    client.on_set_mute = boom

    # Must NOT raise.
    await client.process_message(
        SetMuteState(
            ts=1, trace_id="t", npc_id="npc_setsu", self_mute=True
        ).model_dump_json()
    )


async def test_seat_assigned_join_failure_does_not_propagate() -> None:
    """A discord.connect() failure must be logged but never crash the
    message loop — the WS stays alive so Master can retry."""

    class _Boom(_VcCallbackProbe):
        async def on_join(self) -> None:
            self.join_calls += 1
            raise RuntimeError("vc unavailable")

    probe = _Boom()
    client = _make_client(probe)
    msg = SeatAssigned(
        ts=1, trace_id="t", npc_id="npc_setsu", seat_no=2,
        game_id="g1", phase_id="g1::day1::DAY_DISCUSSION::1",
    )
    # Must NOT raise.
    await client.process_message(msg.model_dump_json())
    assert probe.join_calls == 1
    # State still updated even though VC join failed — Master treats the
    # bot as assigned and the next reconnect will retry the join via
    # the recovery path.
    assert client.assigned_seat == 2


# ------------------------------------------------------ NpcRegistry helpers


def test_registry_unassign_clears_fields() -> None:
    reg = InMemoryNpcRegistry()
    reg.register(
        npc_id="npc_setsu",
        discord_bot_user_id="bot2",
        persona_key="setsu",
        supported_voices=(),
        version="1",
        send=None,
        now_ms=1,
    )
    reg.assign("npc_setsu", seat=2, game_id="g1", phase_id="ph1")
    entry = reg.get("npc_setsu")
    assert entry is not None and entry.assigned_seat == 2

    reg.unassign("npc_setsu")
    entry = reg.get("npc_setsu")
    assert entry is not None
    assert entry.assigned_seat is None
    assert entry.game_id is None
    assert entry.phase_id is None


def test_registry_assigned_to_game_filters_correctly() -> None:
    reg = InMemoryNpcRegistry()
    for idx, key in enumerate(["setsu", "gina", "sq"], start=1):
        reg.register(
            npc_id=f"npc_{key}",
            discord_bot_user_id=f"bot{idx}",
            persona_key=key,
            supported_voices=(),
            version="1",
            send=None,
            now_ms=1,
        )
    reg.assign("npc_setsu", seat=2, game_id="g1", phase_id="ph1")
    reg.assign("npc_gina", seat=3, game_id="g1", phase_id="ph1")
    reg.assign("npc_sq", seat=4, game_id="g2", phase_id="ph2")

    g1 = {e.npc_id for e in reg.assigned_to_game("g1")}
    g2 = {e.npc_id for e in reg.assigned_to_game("g2")}
    g3 = list(reg.assigned_to_game("g3-nonexistent"))

    assert g1 == {"npc_setsu", "npc_gina"}
    assert g2 == {"npc_sq"}
    assert g3 == []


def test_registry_unassign_unknown_id_is_silent() -> None:
    reg = InMemoryNpcRegistry()
    # Must not raise on a missing id.
    reg.unassign("never-registered")


# ------------------------------------------------------ wire round-trip


def test_seat_assigned_pydantic_round_trip() -> None:
    msg = SeatAssigned(
        ts=1,
        trace_id="t",
        npc_id="npc_setsu",
        seat_no=2,
        game_id="g1",
        phase_id="ph",
    )
    out = SeatAssigned.model_validate_json(msg.model_dump_json())
    assert out.npc_id == "npc_setsu"
    assert out.seat_no == 2
    assert out.game_id == "g1"


def test_seat_released_pydantic_round_trip_default_reason() -> None:
    msg = SeatReleased(ts=1, trace_id="t", npc_id="npc_setsu")
    out = SeatReleased.model_validate_json(msg.model_dump_json())
    assert out.npc_id == "npc_setsu"
    assert out.reason == "game_ended"
    assert out.game_id is None
