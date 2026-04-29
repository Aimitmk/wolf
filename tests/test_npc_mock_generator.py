"""MockNpcGenerator behavior tests.

Used when ``NPC_LLM_PROVIDER=mock``. Asserts that the generator returns
persona-appropriate canned phrases via the ``set_persona`` +
``generate`` Protocol that ``make_npc_generator`` expects.
"""

from __future__ import annotations

from wolfbot.domain.ws_messages import LogicPacket, SpeakRequest
from wolfbot.llm.decider_config import LLMDeciderConfig
from wolfbot.npc.generator_factory import make_npc_generator
from wolfbot.npc.mock_generator import MockNpcGenerator


def _logic_packet() -> LogicPacket:
    return LogicPacket(
        ts=1,
        trace_id="t",
        packet_id="lp1",
        phase_id="g::day1::DAY_DISCUSSION::1",
        recipient_npc_id="npc_setsu",
        public_state_summary="phase_id=foo day=1 co_claims=[(none)] silent_seats=[]",
        logic_candidates=(),
        expires_at_ms=2,
    )


def _speak_request() -> SpeakRequest:
    return SpeakRequest(
        ts=1,
        trace_id="t",
        request_id="sr1",
        phase_id="g::day1::DAY_DISCUSSION::1",
        npc_id="npc_setsu",
        seat_no=2,
        logic_packet_id="lp1",
        suggested_intent="speak",
        max_chars=80,
        max_duration_ms=8000,
        priority=0,
        expires_at_ms=2,
    )


async def test_mock_generator_returns_setsu_specific_phrase_after_set_persona() -> None:
    gen = MockNpcGenerator()
    gen.set_persona("setsu")
    speech = await gen.generate(logic=_logic_packet(), request=_speak_request())
    assert speech is not None
    assert speech.intent == "speak"
    # The first phrase in the setsu pool starts with the polite greeting.
    assert "おはよう" in speech.text


async def test_mock_generator_unknown_persona_falls_back_to_generic_pool() -> None:
    gen = MockNpcGenerator()
    gen.set_persona("nonexistent_persona")
    speech = await gen.generate(logic=_logic_packet(), request=_speak_request())
    assert speech is not None
    assert speech.text  # non-empty


async def test_mock_generator_round_robins_within_persona_pool() -> None:
    gen = MockNpcGenerator(scripts={"setsu": ("a", "b", "c")})
    gen.set_persona("setsu")
    out = []
    for _ in range(5):
        speech = await gen.generate(logic=_logic_packet(), request=_speak_request())
        assert speech is not None
        out.append(speech.text)
    assert out == ["a", "b", "c", "a", "b"]


async def test_mock_generator_truncates_text_exceeding_max_chars() -> None:
    long = "あ" * 200
    gen = MockNpcGenerator(scripts={"setsu": (long,)})
    gen.set_persona("setsu")
    req = _speak_request()
    speech = await gen.generate(logic=_logic_packet(), request=req)
    assert speech is not None
    assert len(speech.text) == req.max_chars


def test_make_npc_generator_returns_mock_when_provider_is_mock() -> None:
    cfg = LLMDeciderConfig(provider="mock")
    gen = make_npc_generator(cfg, persona_key="setsu")
    assert isinstance(gen, MockNpcGenerator)
