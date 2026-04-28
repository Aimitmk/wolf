"""Phase-D NPC bot per-game state — snapshot rebuild + update fold.

Covers:
* `state_from_snapshot` rebuilds an `NpcGameState` 1:1 from a snapshot.
* `apply_update` correctly mutates state for each `update_kind`.
* Malformed payloads are dropped without crashing.
* `NpcClient` routes the new WS message types to the state mirror, and
  the decision request handlers reply with the stub abstain/skip
  decisions used until the LLM-backed logic lands.
"""

from __future__ import annotations

import pytest

from wolfbot.domain.ws_messages import (
    DecideNightActionRequest,
    DecideVoteRequest,
    GuardEntry,
    MediumResult,
    NightActionDecision,
    PrivateStateSnapshot,
    PrivateStateUpdate,
    SeerResult,
    VoteDecision,
    WolfChatLine,
)
from wolfbot.npc.client import NpcClient, NpcClientConfig
from wolfbot.npc.game_state import (
    NpcGameState,
    apply_update,
    state_from_snapshot,
)


def _snapshot(**overrides: object) -> PrivateStateSnapshot:
    base: dict[str, object] = {
        "ts": 1000,
        "trace_id": "snap-1",
        "npc_id": "npc_setsu",
        "game_id": "g1",
        "seat_no": 3,
        "persona_key": "setsu",
        "role": "WEREWOLF",
        "day_number": 1,
        "alive_seats": ((1, "Alice"), (3, "セツ"), (5, "Bob")),
        "dead_seats": (),
        "partner_wolves": ((5, "Bob"),),
        "seer_results": (),
        "medium_results": (),
        "guard_history": (),
        "wolf_chat_history": (
            WolfChatLine(
                day=0, speaker_seat=5, speaker_name="Bob", text="夜は静かだな"
            ),
        ),
    }
    base.update(overrides)
    return PrivateStateSnapshot(**base)  # type: ignore[arg-type]


def test_state_from_snapshot_round_trips_fields() -> None:
    snap = _snapshot()
    state = state_from_snapshot(snap)
    assert state.game_id == "g1"
    assert state.seat_no == 3
    assert state.role == "WEREWOLF"
    assert state.persona_key == "setsu"
    assert state.day_number == 1
    assert state.alive_seats == [(1, "Alice"), (3, "セツ"), (5, "Bob")]
    assert state.partner_wolves == [(5, "Bob")]
    assert len(state.wolf_chat_history) == 1
    assert state.wolf_chat_history[0].text == "夜は静かだな"


def test_apply_update_seer_result_appends() -> None:
    state = state_from_snapshot(_snapshot(role="SEER", partner_wolves=()))
    upd = PrivateStateUpdate(
        ts=2000,
        trace_id="upd-1",
        npc_id="npc_setsu",
        game_id="g1",
        seat_no=3,
        update_kind="seer_result",
        payload={
            "day": 1, "target_seat": 5, "target_name": "Bob", "is_wolf": True,
        },
    )
    apply_update(state, upd)
    assert state.seer_results == [
        SeerResult(day=1, target_seat=5, target_name="Bob", is_wolf=True)
    ]


def test_apply_update_medium_result_handles_null_is_wolf() -> None:
    state = state_from_snapshot(_snapshot(role="MEDIUM", partner_wolves=()))
    upd = PrivateStateUpdate(
        ts=3000, trace_id="t", npc_id="npc_setsu", game_id="g1", seat_no=3,
        update_kind="medium_result",
        payload={"day": 1, "target_seat": 7, "target_name": "Carol", "is_wolf": None},
    )
    apply_update(state, upd)
    assert state.medium_results == [
        MediumResult(day=1, target_seat=7, target_name="Carol", is_wolf=None)
    ]


def test_apply_update_guard_resolved_fills_peaceful_flag() -> None:
    state = state_from_snapshot(
        _snapshot(
            role="KNIGHT",
            partner_wolves=(),
            guard_history=(
                GuardEntry(day=1, target_seat=5, target_name="Bob"),
            ),
        )
    )
    upd = PrivateStateUpdate(
        ts=4000, trace_id="t", npc_id="npc_setsu", game_id="g1", seat_no=3,
        update_kind="guard_resolved",
        payload={"day": 1, "peaceful_morning": True},
    )
    apply_update(state, upd)
    assert state.guard_history[0].peaceful_morning is True


def test_apply_update_wolf_chat_appends() -> None:
    state = state_from_snapshot(_snapshot())
    upd = PrivateStateUpdate(
        ts=5000, trace_id="t", npc_id="npc_setsu", game_id="g1", seat_no=3,
        update_kind="wolf_chat",
        payload={
            "day": 1, "speaker_seat": 5, "speaker_name": "Bob", "text": "席1を狙おう",
        },
    )
    apply_update(state, upd)
    assert state.wolf_chat_history[-1].text == "席1を狙おう"


def test_apply_update_alive_changed_replaces_lists() -> None:
    state = state_from_snapshot(_snapshot())
    upd = PrivateStateUpdate(
        ts=6000, trace_id="t", npc_id="npc_setsu", game_id="g1", seat_no=3,
        update_kind="alive_changed",
        payload={
            "alive_seats": [[1, "Alice"], [3, "セツ"]],
            "dead_seats": [[5, "Bob"]],
        },
    )
    apply_update(state, upd)
    assert state.alive_seats == [(1, "Alice"), (3, "セツ")]
    assert state.dead_seats == [(5, "Bob")]


def test_apply_update_day_advanced_updates_counter() -> None:
    state = state_from_snapshot(_snapshot())
    upd = PrivateStateUpdate(
        ts=7000, trace_id="t", npc_id="npc_setsu", game_id="g1", seat_no=3,
        update_kind="day_advanced", payload={"day_number": 2},
    )
    apply_update(state, upd)
    assert state.day_number == 2


def test_apply_update_malformed_payload_drops_silently() -> None:
    state = state_from_snapshot(_snapshot())
    before = list(state.seer_results)
    upd = PrivateStateUpdate(
        ts=8000, trace_id="t", npc_id="npc_setsu", game_id="g1", seat_no=3,
        update_kind="seer_result",
        payload={"day": "not-a-number", "target_seat": 5},
    )
    apply_update(state, upd)
    assert state.seer_results == before


def test_apply_update_unknown_kind_is_noop() -> None:
    state = state_from_snapshot(_snapshot())
    snapshot_before = (
        list(state.seer_results),
        list(state.wolf_chat_history),
        state.day_number,
    )
    # Bypass Pydantic's Literal validation by reaching for `model_construct`
    # — emulates a Master that emits a future ``update_kind`` we don't know.
    upd = PrivateStateUpdate.model_construct(
        ts=9000, trace_id="t", type="private_state_update",
        npc_id="npc_setsu", game_id="g1", seat_no=3,
        update_kind="future_kind_not_yet_defined",  # type: ignore[arg-type]
        payload={"foo": "bar"},
    )
    apply_update(state, upd)
    assert (
        list(state.seer_results),
        list(state.wolf_chat_history),
        state.day_number,
    ) == snapshot_before


# --------------------------------------------- NpcClient WS routing


def _make_client_with_capture() -> tuple[NpcClient, list[str]]:
    """Lightweight NpcClient + outbound capture buffer.

    The phase-D message paths only use `send`, `now_ms`, and `config.npc_id`,
    so the heavier collaborators (TTS, playback, speech service) can be
    minimal stubs — they're never exercised by these tests.
    """
    sent: list[str] = []

    async def _send(msg: str) -> None:
        sent.append(msg)

    class _StubSpeech:
        async def respond(self, **kwargs: object) -> None:  # pragma: no cover
            raise AssertionError("speech service should not be invoked")

    class _StubTts:
        async def synthesize(self, *args: object, **kwargs: object) -> None:  # pragma: no cover
            raise AssertionError("TTS should not be invoked")

    class _StubPlayback:
        async def play(self, **kwargs: object) -> tuple[int, int]:  # pragma: no cover
            raise AssertionError("playback should not be invoked")

    client = NpcClient(
        config=NpcClientConfig(
            npc_id="npc_setsu",
            discord_bot_user_id="bot1",
            persona_key="setsu",
            voice_id="2",
        ),
        speech=_StubSpeech(),  # type: ignore[arg-type]
        tts=_StubTts(),  # type: ignore[arg-type]
        playback=_StubPlayback(),  # type: ignore[arg-type]
        send=_send,
        now_ms=lambda: 999,
    )
    return client, sent


@pytest.mark.asyncio
async def test_client_processes_snapshot_and_update_and_decision_requests() -> None:
    client, sent = _make_client_with_capture()

    await client.process_message(_snapshot().model_dump_json())
    assert "g1" in client.game_states
    assert client.game_states["g1"].role == "WEREWOLF"

    upd = PrivateStateUpdate(
        ts=2000, trace_id="t", npc_id="npc_setsu", game_id="g1", seat_no=3,
        update_kind="day_advanced", payload={"day_number": 4},
    )
    await client.process_message(upd.model_dump_json())
    assert client.game_states["g1"].day_number == 4

    vote_req = DecideVoteRequest(
        ts=3000, trace_id="t-vote",
        request_id="rv1", npc_id="npc_setsu", seat_no=3,
        game_id="g1", phase_id="g1::day1::DAY_VOTE::1",
        candidate_seats=((1, "Alice"), (5, "Bob")),
        expires_at_ms=10_000,
    )
    await client.process_message(vote_req.model_dump_json())
    decisions = [VoteDecision.model_validate_json(m) for m in sent if '"vote_decision"' in m]
    assert len(decisions) == 1
    assert decisions[0].request_id == "rv1"
    assert decisions[0].target_seat is None  # phase-D stub abstains for now

    night_req = DecideNightActionRequest(
        ts=4000, trace_id="t-night",
        request_id="rn1", npc_id="npc_setsu", seat_no=3,
        game_id="g1", phase_id="g1::day1::NIGHT::1",
        action_kind="wolf_attack",
        candidate_seats=((1, "Alice"),),
        expires_at_ms=20_000,
    )
    await client.process_message(night_req.model_dump_json())
    night_decisions = [
        NightActionDecision.model_validate_json(m) for m in sent if '"night_action_decision"' in m
    ]
    assert len(night_decisions) == 1
    assert night_decisions[0].request_id == "rn1"
    assert night_decisions[0].target_seat is None
    assert night_decisions[0].action_kind == "wolf_attack"


@pytest.mark.asyncio
async def test_client_drops_misrouted_state_messages() -> None:
    client, _sent = _make_client_with_capture()
    other = _snapshot(npc_id="npc_other")
    await client.process_message(other.model_dump_json())
    assert "g1" not in client.game_states


@pytest.mark.asyncio
async def test_client_drops_update_when_no_snapshot_seen() -> None:
    client, _sent = _make_client_with_capture()
    upd = PrivateStateUpdate(
        ts=5000, trace_id="t", npc_id="npc_setsu", game_id="never_snapshotted",
        seat_no=3, update_kind="day_advanced", payload={"day_number": 9},
    )
    await client.process_message(upd.model_dump_json())
    assert "never_snapshotted" not in client.game_states


def test_npcgamestate_constructs_with_defaults() -> None:
    """Sanity: the dataclass instantiates with defaults so test fixtures
    can build empty state without going through a snapshot."""
    state = NpcGameState(
        game_id="g", seat_no=1, persona_key="setsu", role="VILLAGER"
    )
    assert state.game_id == "g"
    assert state.seer_results == []
