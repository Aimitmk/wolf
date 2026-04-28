"""NPC seat assignment + OpenAICompatibleNpcGenerator prompt-building tests.

Verifies:
- _on_reactive_phase_enter assigns online NPCs to LLM seats.
- Duplicate assignments are avoided (idempotent on re-enter).
- Fewer NPCs than seats → partial assignment.
- Fewer seats than NPCs → capped to seat count.
- OpenAICompatibleNpcGenerator prompt construction (system + user messages).
- OpenAICompatibleNpcGenerator skip intent returns None.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from wolfbot.domain.discussion import make_phase_id
from wolfbot.domain.enums import Phase, Role
from wolfbot.domain.models import Game, Seat
from wolfbot.domain.ws_messages import LogicCandidate, LogicPacket, SpeakRequest
from wolfbot.master.npc_registry import InMemoryNpcRegistry
from wolfbot.npc.openai_compatible_generator import (
    _build_system,
    _build_user,
    _format_candidate,
)
from wolfbot.npc.personas import NPC_PERSONAS_BY_KEY as PERSONAS_BY_KEY
from wolfbot.persistence.sqlite_repo import SqliteRepo


def _noop_send(buf: list[str]) -> Callable[[str], Awaitable[None]]:
    async def send(msg: str) -> None:
        buf.append(msg)
    return send


async def _seed_game_3llm(repo: SqliteRepo) -> tuple[Game, list[Seat]]:
    g = Game(
        id="sa1",
        guild_id="gu",
        host_user_id="h",
        phase=Phase.DAY_DISCUSSION,
        day_number=1,
        main_text_channel_id="c1",
        main_vc_channel_id="c2",
        created_at=0,
    )
    await repo.create_game(g)
    seats = [
        Seat(seat_no=1, display_name="Alice",
             discord_user_id="u1", is_llm=False, persona_key=None),
        Seat(seat_no=2, display_name="🌙 セツ", discord_user_id=None,
             is_llm=True, persona_key="setsu"),
        Seat(seat_no=3, display_name="🔴 ジナ", discord_user_id=None,
             is_llm=True, persona_key="gina"),
        Seat(seat_no=4, display_name="🟦 SQ",
             discord_user_id=None, is_llm=True, persona_key="sq"),
    ]
    for s in seats:
        await repo.insert_seat(g.id, s)
    for s in seats:
        await repo.set_player_role(g.id, s.seat_no, Role.VILLAGER)
    return g, seats


# ---- Seat assignment logic (mirrors _on_reactive_phase_enter in main.py) ----

async def _run_assignment(
    repo: SqliteRepo,
    registry: InMemoryNpcRegistry,
    game_id: str,
    phase_id: str,
) -> None:
    """Reproduce the assignment logic from main._on_reactive_phase_enter."""
    seats = await repo.load_seats(game_id)
    llm_seats = [s for s in seats if s.is_llm]
    online = registry.all_online()
    assigned_npc_ids = {
        e.npc_id for e in online
        if e.assigned_seat is not None and e.game_id == game_id
    }
    unassigned_npcs = [e for e in online if e.npc_id not in assigned_npc_ids]
    unassigned_seats = [
        s for s in llm_seats
        if not any(e.assigned_seat == s.seat_no and e.game_id == game_id for e in online)
    ]
    for npc_entry, seat in zip(unassigned_npcs, unassigned_seats, strict=False):
        registry.assign(
            npc_entry.npc_id,
            seat=seat.seat_no,
            game_id=game_id,
            phase_id=phase_id,
        )


async def test_assigns_online_npcs_to_llm_seats(repo: SqliteRepo) -> None:
    game, _seats = await _seed_game_3llm(repo)
    registry = InMemoryNpcRegistry()
    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)

    for i, npc_id in enumerate(["npc_a", "npc_b", "npc_c"]):
        registry.register(
            npc_id=npc_id,
            discord_bot_user_id=f"bot{i}",
            supported_voices=(),
            version="1",
            send=_noop_send([]),
            now_ms=1000, persona_key="setsu")

    await _run_assignment(repo, registry, game.id, phase_id)

    online = registry.all_online()
    assigned = {
        e.npc_id: e.assigned_seat for e in online if e.assigned_seat is not None}
    assert len(assigned) == 3
    assert set(assigned.values()) == {2, 3, 4}


async def test_idempotent_reenter_does_not_reassign(repo: SqliteRepo) -> None:
    game, _ = await _seed_game_3llm(repo)
    registry = InMemoryNpcRegistry()
    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)

    registry.register(
        npc_id="npc_x",
        discord_bot_user_id="botx",
        supported_voices=(),
        version="1",
        send=_noop_send([]),
        now_ms=1000, persona_key="setsu")

    await _run_assignment(repo, registry, game.id, phase_id)
    entry = registry.get("npc_x")
    assert entry is not None and entry.assigned_seat is not None
    first_seat = entry.assigned_seat

    # Re-run: should NOT change the assignment.
    await _run_assignment(repo, registry, game.id, phase_id)
    assert entry.assigned_seat == first_seat


async def test_fewer_npcs_than_seats_partial_assignment(repo: SqliteRepo) -> None:
    game, _ = await _seed_game_3llm(repo)
    registry = InMemoryNpcRegistry()
    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)

    # Only 1 NPC for 3 LLM seats
    registry.register(
        npc_id="npc_only",
        discord_bot_user_id="bot_only",
        supported_voices=(),
        version="1",
        send=_noop_send([]),
        now_ms=1000, persona_key="setsu")

    await _run_assignment(repo, registry, game.id, phase_id)
    entry = registry.get("npc_only")
    assert entry is not None and entry.assigned_seat is not None
    # Only 1 assignment total
    assigned_count = sum(
        1 for e in registry.all_online() if e.assigned_seat is not None
    )
    assert assigned_count == 1


async def test_more_npcs_than_seats_capped(repo: SqliteRepo) -> None:
    game, _ = await _seed_game_3llm(repo)
    registry = InMemoryNpcRegistry()
    phase_id = make_phase_id(game.id, 1, Phase.DAY_DISCUSSION)

    # 5 NPCs for 3 LLM seats
    for i in range(5):
        registry.register(
            npc_id=f"npc_{i}",
            discord_bot_user_id=f"bot_{i}",
            supported_voices=(),
            version="1",
            send=_noop_send([]),
            now_ms=1000, persona_key="setsu")

    await _run_assignment(repo, registry, game.id, phase_id)
    assigned_count = sum(
        1 for e in registry.all_online() if e.assigned_seat is not None
    )
    assert assigned_count == 3  # capped at LLM seat count


# ---- OpenAICompatibleNpcGenerator prompt-building unit tests ----


def test_build_system_prompt_contains_persona_fields() -> None:
    persona = PERSONAS_BY_KEY["setsu"]
    sys_msg = _build_system(persona, max_chars=80)
    assert persona.display_name in sys_msg
    assert persona.speech_profile.first_person in sys_msg
    assert "80" in sys_msg
    assert "日本語" in sys_msg


def test_build_user_prompt_includes_logic_candidates() -> None:
    logic = LogicPacket(
        ts=1,
        trace_id="t",
        packet_id="lp",
        phase_id="ph",
        recipient_npc_id="npc_1",
        public_state_summary="alive=[1,2,3]",
        logic_candidates=(
            LogicCandidate(
                id="c1",
                claim="席2が占い師COした",
                support=("初日白出し",),
                counter=("対抗なし",),
            ),
        ),
        pressure={"1": 0.3, "3": 0.7},
        expires_at_ms=9999,
    )
    request = SpeakRequest(
        ts=1,
        trace_id="t",
        request_id="sr1",
        npc_id="npc_1",
        phase_id="ph",
        seat_no=2,
        logic_packet_id="lp",
        suggested_intent="speak",
        max_chars=80,
        expires_at_ms=5000,
    )
    user_msg = _build_user(logic, request)
    assert "占い師CO" in user_msg
    assert "初日白出し" in user_msg
    assert "対抗なし" in user_msg
    assert "alive=[1,2,3]" in user_msg
    assert "圧力マップ" in user_msg


def test_format_candidate_with_all_fields() -> None:
    c = LogicCandidate(
        id="c2",
        claim="主張テスト",
        support=("根拠A", "根拠B"),
        counter=("反論X",),
    )
    out = _format_candidate(c)
    assert "[c2]" in out
    assert "主張テスト" in out
    assert "根拠A" in out
    assert "反論X" in out


def test_build_system_prompt_includes_full_persona_and_optional_role() -> None:
    """The reactive_voice NPC system prompt mirrors rounds-mode by rendering
    the full speech_profile + judgment_profile for the persona, and surfaces
    the role + role_strategy when Master sends them on the SpeakRequest.
    """
    persona = PERSONAS_BY_KEY["setsu"]

    # Without role/role_strategy: still has full persona blocks.
    sys_no_role = _build_system(persona, max_chars=80)
    sp = persona.speech_profile
    assert sp.address_style in sys_no_role
    assert "判断のクセ" in sys_no_role
    # Bands rendered from judgment_profile axes:
    assert "攻撃性" in sys_no_role and "流れへの追従度" in sys_no_role
    assert "あなたの役職" not in sys_no_role
    assert "戦術ヒント" not in sys_no_role

    # With role + strategy.
    sys_with_role = _build_system(
        persona,
        max_chars=80,
        role="SEER",
        role_strategy="占い師の戦術メモ\n- 結果は出す",
    )
    assert "SEER" in sys_with_role
    assert "占い師の戦術メモ" in sys_with_role


def test_build_system_prompt_silent_gesture_persona() -> None:
    """Kukrushka's `narration_mode=silent_gesture` must round-trip through
    the NPC system prompt — previously dropped because the historical
    `_build_system` only handled the standard speech profile fields.
    """
    persona = PERSONAS_BY_KEY["kukrushka"]
    sys_msg = _build_system(persona, max_chars=80)
    # The silent_gesture branch in build_speech_profile_block emits this
    # exact label; if it's missing the NPC will speak normally.
    assert "ほぼ無言" in sys_msg or "叙述モード" in sys_msg


def test_build_user_prompt_renders_recent_speeches_and_seats() -> None:
    """Recent speeches + alive/dead seats from the SpeakRequest land in
    the user prompt with the speaker's display name attached."""
    from wolfbot.domain.ws_messages import RecentSpeech

    logic = LogicPacket(
        ts=1,
        trace_id="t",
        packet_id="lp",
        phase_id="ph",
        recipient_npc_id="npc_1",
        public_state_summary="alive=[1,2,3]",
        recent_speeches=(
            RecentSpeech(
                seat_no=1,
                display_name="Alice",
                source="text",
                text="占いの結果が気になる",
            ),
            RecentSpeech(
                seat_no=3,
                display_name="🌙セツ",
                source="npc_generated",
                text="まだ静かですね",
            ),
        ),
        expires_at_ms=9999,
    )
    request = SpeakRequest(
        ts=1,
        trace_id="t",
        request_id="sr",
        npc_id="npc_1",
        phase_id="ph",
        seat_no=2,
        logic_packet_id="lp",
        suggested_intent="speak",
        max_chars=80,
        expires_at_ms=5000,
        alive_seats=((1, "Alice"), (2, "Bob"), (3, "🌙セツ")),
        dead_seats=((4, "故人"),),
    )
    user_msg = _build_user(logic, request)
    assert "## 直近の発言" in user_msg
    assert "席1 Alice" in user_msg and "占いの結果が気になる" in user_msg
    assert "席3 🌙セツ" in user_msg and "まだ静かですね" in user_msg
    assert "[テキスト]" in user_msg and "[NPC発話]" in user_msg
    assert "## 生存者" in user_msg
    assert "席1 Alice、席2 Bob、席3 🌙セツ" in user_msg
    assert "## 死亡者" in user_msg and "席4 故人" in user_msg


def test_build_user_prompt_no_candidates_no_pressure() -> None:
    logic = LogicPacket(
        ts=1,
        trace_id="t",
        packet_id="lp2",
        phase_id="ph",
        recipient_npc_id="npc_1",
        public_state_summary="quiet",
        logic_candidates=(),
        pressure={},
        expires_at_ms=9999,
    )
    request = SpeakRequest(
        ts=1,
        trace_id="t",
        request_id="sr2",
        npc_id="npc_1",
        phase_id="ph",
        seat_no=2,
        logic_packet_id="lp2",
        suggested_intent="question",
        max_chars=80,
        expires_at_ms=5000,
    )
    user_msg = _build_user(logic, request)
    assert "quiet" in user_msg
    assert "論点候補" not in user_msg
    assert "圧力マップ" not in user_msg
