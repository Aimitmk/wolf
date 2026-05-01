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


@pytest.mark.asyncio
async def test_vote_target_falls_back_to_random_when_llm_returns_null() -> None:
    """Even though the schema forbids null, defensive coverage: if the
    decision LLM returns ``target_seat=None`` (parse error, persona
    inertia, etc.) the NPC must still vote — pick a deterministic-but-
    pseudo-random candidate from the legal set, never abstain.
    """
    client, sent = _make_client_with_capture()
    await client.process_message(_snapshot().model_dump_json())

    class _StubDecisionLLM:
        async def decide_json(
            self, *, system_prompt: str, user_prompt: str,
            schema: dict[str, object],
        ) -> str:
            # Simulate an abstain response slipping through.
            return '{"target_seat": null, "reason": "情報不足"}'

    client.decision_llm = _StubDecisionLLM()  # type: ignore[assignment]

    vote_req = DecideVoteRequest(
        ts=3000, trace_id="t-vote",
        request_id="rv-fallback", npc_id="npc_setsu", seat_no=3,
        game_id="g1", phase_id="g1::day1::DAY_VOTE::1",
        candidate_seats=((1, "Alice"), (5, "Bob")),
        expires_at_ms=10_000,
    )
    await client.process_message(vote_req.model_dump_json())
    decisions = [VoteDecision.model_validate_json(m) for m in sent if '"vote_decision"' in m]
    assert len(decisions) == 1
    assert decisions[0].target_seat in {1, 5}, (
        "abstain must be replaced by a legal candidate via the fallback"
    )
    assert decisions[0].reason_summary is not None
    assert "abstain_fallback" in decisions[0].reason_summary


@pytest.mark.asyncio
async def test_night_target_falls_back_to_random_when_llm_returns_null() -> None:
    """Master rejects night actions with `target_seat=None` (ILLEGAL_TARGET),
    and the missing seat deadlocks the NIGHT phase via pending_decisions.
    Live game stalled when the knight chose to skip the day-1 guard
    ("GJ リスク回避"). Force a legal pick so the phase always advances.
    """
    client, sent = _make_client_with_capture()
    await client.process_message(_snapshot(role="KNIGHT").model_dump_json())

    class _StubDecisionLLM:
        async def decide_json(
            self, *, system_prompt: str, user_prompt: str,
            schema: dict[str, object],
        ) -> str:
            # Knight tries to skip — must NOT be allowed through.
            return '{"target_seat": null, "reason": "情報不足のため次夜余地残す"}'

    client.decision_llm = _StubDecisionLLM()  # type: ignore[assignment]

    night_req = DecideNightActionRequest(
        ts=4000, trace_id="t-night",
        request_id="rn-fallback", npc_id="npc_setsu", seat_no=3,
        game_id="g1", phase_id="g1::day1::NIGHT::1",
        action_kind="knight_guard",
        candidate_seats=((1, "Alice"), (5, "Bob")),
        expires_at_ms=20_000,
    )
    await client.process_message(night_req.model_dump_json())
    decisions = [
        NightActionDecision.model_validate_json(m)
        for m in sent if '"night_action_decision"' in m
    ]
    assert len(decisions) == 1
    assert decisions[0].target_seat in {1, 5}, (
        "skip must be replaced by a legal candidate via the fallback"
    )
    assert decisions[0].reason_summary is not None
    assert "abstain_fallback" in decisions[0].reason_summary


@pytest.mark.asyncio
async def test_alive_changed_update_carries_dead_seat_causes() -> None:
    """`alive_changed` payload now propagates per-seat death cause so
    the NPC prompt can label dead seats as 処刑/襲撃."""
    client, _sent = _make_client_with_capture()
    await client.process_message(_snapshot().model_dump_json())
    upd = PrivateStateUpdate(
        ts=2000, trace_id="t", npc_id="npc_setsu", game_id="g1", seat_no=3,
        update_kind="alive_changed",
        payload={
            "alive_seats": [[1, "Alice"], [3, "セツ"]],
            "dead_seats": [[5, "Bob"], [8, "Stella"]],
            "dead_seat_causes": [[5, "EXECUTION"], [8, "ATTACK"]],
        },
    )
    await client.process_message(upd.model_dump_json())
    state = client.game_states["g1"]
    assert state.dead_seat_causes == {5: "EXECUTION", 8: "ATTACK"}


@pytest.mark.asyncio
async def test_seat_released_drops_per_game_state_and_logic_cache() -> None:
    """`_on_seat_released` is the long-term cleanup hook for an NPC bot
    that plays many games in one process. Without it `game_states` and
    the LogicPacket cache grow unbounded across games.

    This exercises both:
    - `game_states[game_id]` is popped (but other games stay).
    - `_logic_cache` entries whose phase_id starts with the released
      game_id are dropped (other games preserved).
    """
    from wolfbot.domain.ws_messages import LogicPacket, SeatReleased

    client, _sent = _make_client_with_capture()
    # Seed state for two games and two logic packets.
    await client.process_message(_snapshot(game_id="g1").model_dump_json())
    await client.process_message(_snapshot(game_id="g2").model_dump_json())
    assert "g1" in client.game_states and "g2" in client.game_states

    pkt_g1 = LogicPacket(
        ts=1, trace_id="t",
        packet_id="lp_aaa", phase_id="g1::day1::DAY_DISCUSSION::1",
        recipient_npc_id="npc_setsu",
        public_state_summary="(d)", expires_at_ms=9999,
    )
    pkt_g2 = LogicPacket(
        ts=1, trace_id="t",
        packet_id="lp_bbb", phase_id="g2::day1::DAY_DISCUSSION::1",
        recipient_npc_id="npc_setsu",
        public_state_summary="(d)", expires_at_ms=9999,
    )
    client._logic_cache[pkt_g1.packet_id] = pkt_g1
    client._logic_cache[pkt_g2.packet_id] = pkt_g2

    # Release g1.
    msg = SeatReleased(
        ts=2000, trace_id="rel-g1",
        npc_id="npc_setsu", game_id="g1", reason="game_ended",
    )
    await client.process_message(msg.model_dump_json())

    assert "g1" not in client.game_states, "released game state must be dropped"
    assert "g2" in client.game_states, "other-game state must be preserved"
    assert "lp_aaa" not in client._logic_cache
    assert "lp_bbb" in client._logic_cache


def test_npcgamestate_constructs_with_defaults() -> None:
    """Sanity: the dataclass instantiates with defaults so test fixtures
    can build empty state without going through a snapshot."""
    state = NpcGameState(
        game_id="g", seat_no=1, persona_key="setsu", role="VILLAGER"
    )
    assert state.game_id == "g"
    assert state.seer_results == []


@pytest.mark.asyncio
async def test_vote_falls_back_when_llm_raises_exception() -> None:
    """Vertex AI sometimes returns 504 DEADLINE_EXCEEDED / 429
    RESOURCE_EXHAUSTED. The previous behaviour returned None on
    exception, bypassing the abstain fallback and silently dropping
    the ballot. Force a legal pick so the seat doesn't end up in the
    silent-abstain bucket on a transient LLM failure.
    """
    client, sent = _make_client_with_capture()
    await client.process_message(_snapshot().model_dump_json())

    class _RaisingLLM:
        async def decide_json(
            self, *, system_prompt: str, user_prompt: str,
            schema: dict[str, object],
        ) -> str:
            raise RuntimeError("simulated 504 DEADLINE_EXCEEDED")

    client.decision_llm = _RaisingLLM()  # type: ignore[assignment]

    vote_req = DecideVoteRequest(
        ts=3000, trace_id="t-vote",
        request_id="rv-llm-err", npc_id="npc_setsu", seat_no=3,
        game_id="g1", phase_id="g1::day1::DAY_VOTE::1",
        candidate_seats=((1, "Alice"), (5, "Bob")),
        expires_at_ms=10_000,
    )
    await client.process_message(vote_req.model_dump_json())
    decisions = [VoteDecision.model_validate_json(m) for m in sent if '"vote_decision"' in m]
    assert len(decisions) == 1
    assert decisions[0].target_seat in {1, 5}, (
        "LLM exception must trigger abstain fallback, not silent abstain"
    )
    assert decisions[0].reason_summary is not None
    assert "abstain_fallback" in decisions[0].reason_summary


@pytest.mark.asyncio
async def test_night_target_falls_back_when_llm_raises_exception() -> None:
    """Reproduces game `06c38cd43494` NIGHT_1 stall: Vertex AI returned
    504 DEADLINE_EXCEEDED on the seer's divine call → previous code
    short-circuited with `None, "llm_error"` → Master's
    `pending_decisions` retained `missing_seats=[seer]` past the
    deadline → game parked in WAITING_HOST_DECISION. Now the random-
    legal fallback covers transport errors too.
    """
    client, sent = _make_client_with_capture()
    await client.process_message(_snapshot(role="SEER").model_dump_json())

    class _RaisingLLM:
        async def decide_json(
            self, *, system_prompt: str, user_prompt: str,
            schema: dict[str, object],
        ) -> str:
            raise RuntimeError("simulated 504 DEADLINE_EXCEEDED")

    client.decision_llm = _RaisingLLM()  # type: ignore[assignment]

    night_req = DecideNightActionRequest(
        ts=4000, trace_id="t-night",
        request_id="rn-llm-err", npc_id="npc_setsu", seat_no=3,
        game_id="g1", phase_id="g1::day1::NIGHT::1",
        action_kind="seer_divine",
        candidate_seats=((1, "Alice"), (5, "Bob")),
        expires_at_ms=20_000,
    )
    await client.process_message(night_req.model_dump_json())
    decisions = [
        NightActionDecision.model_validate_json(m)
        for m in sent if '"night_action_decision"' in m
    ]
    assert len(decisions) == 1
    assert decisions[0].target_seat in {1, 5}, (
        "LLM exception must trigger abstain fallback for night actions too"
    )
    assert decisions[0].reason_summary is not None
    assert "abstain_fallback" in decisions[0].reason_summary
