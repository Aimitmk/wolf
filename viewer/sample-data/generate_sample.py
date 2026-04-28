"""Generate ``game-sample.json`` — a hand-crafted 1-game demo for the viewer.

The synthetic game is a 9-player 2-day village victory:

- Day 1: village votes out wolf #1 (seat 3, Jina, fake-seer counter-CO)
- Night 1: wolf #2 attacks the real seer; knight guard succeeds; seer divines wolf #2
- Day 2: village pieces it together, votes out wolf #2

Includes:
- full SQLite-shape game state (games, seats, public/private logs, votes,
  night_actions, speech_events)
- LLM trace lines (gameplay + npc_speech + voice_stt) with realistic
  prompt / response / token / latency stubs

The output file is the single source of truth for the Next.js viewer;
the viewer reads ``viewer/sample-data/game-sample.json`` by default.

Usage:
    uv run python viewer/sample-data/generate_sample.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

random.seed(42)

GAME_ID = "g_sample_001"
GUILD_ID = "1498215601808867421"
HOST_USER_ID = "146810000000000001"
CREATED_AT_MS = 1714291200000  # 2026-04-28T08:00:00Z
DAY1_START_MS = CREATED_AT_MS + 60_000
NIGHT0_START_MS = DAY1_START_MS - 30_000

# ─── seats ────────────────────────────────────────────────────────────────
# Roles: 2 wolves, 1 madman, 1 seer, 1 medium, 1 knight, 3 villagers
SEATS = [
    {"seat_no": 1, "display_name": "あなた", "is_llm": False, "persona_key": None,
     "discord_user_id": HOST_USER_ID, "role": "VILLAGER"},
    {"seat_no": 2, "display_name": "🌙 セツ", "is_llm": True, "persona_key": "setsu",
     "discord_user_id": "1498225401372086313", "role": "SEER"},
    {"seat_no": 3, "display_name": "🍷 ジーナ", "is_llm": True, "persona_key": "gina",
     "discord_user_id": "1498225401372086314", "role": "WEREWOLF"},
    {"seat_no": 4, "display_name": "🎲 SQ", "is_llm": True, "persona_key": "sq",
     "discord_user_id": "1498225401372086315", "role": "VILLAGER"},
    {"seat_no": 5, "display_name": "🔮 ラキオ", "is_llm": True, "persona_key": "raqio",
     "discord_user_id": "1498225401372086316", "role": "MEDIUM"},
    {"seat_no": 6, "display_name": "🛡 ステラ", "is_llm": True, "persona_key": "stella",
     "discord_user_id": "1498225401372086317", "role": "KNIGHT"},
    {"seat_no": 7, "display_name": "⚔ シゲミチ", "is_llm": True, "persona_key": "shigemichi",
     "discord_user_id": "1498225401372086318", "role": "WEREWOLF"},
    {"seat_no": 8, "display_name": "☄ コメット", "is_llm": True, "persona_key": "comet",
     "discord_user_id": "1498225401372086319", "role": "MADMAN"},
    {"seat_no": 9, "display_name": "📐 ジョナス", "is_llm": True, "persona_key": "jonas",
     "discord_user_id": "1498225401372086320", "role": "VILLAGER"},
]


def _seat_token(seat_no: int) -> str:
    s = next(s for s in SEATS if s["seat_no"] == seat_no)
    return f"席{seat_no} {s['display_name']}"


def _is_alive_at(seat_no: int, day: int, phase: str) -> bool:
    # Final state after game: wolves (3, 7) dead.
    # Day 1 vote kills 3. Night 1: knight guards seer, no death. Day 2 vote kills 7.
    if seat_no == 3:
        return not (day >= 1 and phase in {"DAY_VOTE", "NIGHT", "DAY_DISCUSSION", "GAME_OVER"} and day >= 1) or (
            day == 1 and phase == "DAY_VOTE"
        )
    return True


# ─── public log / phase timeline ──────────────────────────────────────────
def _ts(offset_s: int) -> int:
    return DAY1_START_MS + offset_s * 1000


def build_phases() -> list[dict[str, Any]]:
    return [
        {
            "day": 0,
            "phase": "SETUP",
            "started_at_ms": NIGHT0_START_MS,
            "public_logs": [
                {
                    "kind": "SETUP_COMPLETE",
                    "actor_seat": None,
                    "text": "参加者 9 名で人狼ゲームを開始します。役職は各DMをご確認ください。",
                    "created_at_ms": NIGHT0_START_MS,
                },
            ],
            "speech_events": [],
            "votes": [],
            "night_actions": [],
        },
        {
            "day": 0,
            "phase": "NIGHT_0",
            "started_at_ms": NIGHT0_START_MS + 5000,
            "public_logs": [],
            "speech_events": [],
            "votes": [],
            "night_actions": [
                {
                    "day": 0,
                    "actor_seat": 2,
                    "kind": "DIVINE_NIGHT0_RANDOM_WHITE",
                    "target_seat": 9,
                    "submitted_at_ms": NIGHT0_START_MS + 8000,
                },
            ],
        },
        {
            "day": 1,
            "phase": "DAY_DISCUSSION",
            "started_at_ms": _ts(0),
            "public_logs": [
                {
                    "kind": "PHASE_CHANGE",
                    "actor_seat": None,
                    "text": "夜が明けました。1 日目の議論を開始致します。制限時間は 300 秒でございます。",
                    "created_at_ms": _ts(0),
                },
            ],
            "speech_events": [
                _speech(2, "text", _ts(15), "占い師COします。初日ランダム白で席9 ジョナスを占いました。白判定です。",
                        co_declaration="seer", summary="seat 2 COs seer; seat 9 white"),
                _speech(3, "text", _ts(30), "私も占い師です。席4 SQを占って白でした。席2は何かおかしい。",
                        co_declaration="seer", summary="seat 3 counter-COs seer; seat 4 white"),
                _speech(5, "text", _ts(50), "霊媒師COはまだしません。今日処刑が出てから結果を出します。",
                        summary="seat 5 plans medium CO after first execution"),
                _speech(9, "text", _ts(70), "席2の白判定をもらった9番です。占い理由が筋が通ってる。", summary=None),
                _speech(4, "text", _ts(90), "占いが2人いる。どちらが本物か議論したい。", summary=None),
                _speech(6, "text", _ts(110), "占いCOの2人をしっかり見極めましょう。", summary=None),
                _speech(7, "text", _ts(130), "席3が偽のように見える。占い理由が雑な印象。", summary=None),
                _speech(8, "text", _ts(150), "私は席2が怪しいと感じる。席3を信じたい。", summary=None),
                _speech(1, "text", _ts(170), "席3の占い理由がパッとしない。席2の方が信用できそう。", summary=None),
                # Round 2
                _speech(2, "text", _ts(200), "席3の理由が薄いです。席3を投票候補にしたい。", summary="seat 2 nominates seat 3"),
                _speech(3, "text", _ts(215), "席2が偽です。みなさん席2に投票してください。", summary="seat 3 nominates seat 2"),
                _speech(5, "text", _ts(230), "占い騙りなら2が真の可能性が高い気がする。", summary=None),
                _speech(9, "text", _ts(250), "席2に白を貰った立場として、席3が偽だと考える。", summary=None),
            ],
            "votes": [],
            "night_actions": [],
        },
        {
            "day": 1,
            "phase": "DAY_VOTE",
            "started_at_ms": _ts(300),
            "public_logs": [
                {
                    "kind": "PHASE_CHANGE",
                    "actor_seat": None,
                    "text": "議論時間が終了しました。投票フェイズへ移行致します。制限時間は 60 秒でございます。",
                    "created_at_ms": _ts(300),
                },
                {
                    "kind": "EXECUTION",
                    "actor_seat": 3,
                    "text": "席3 🍷 ジーナ が処刑されました。\n\n投票内訳: 席3=5票 / 席2=3票 / 席3=1票",
                    "created_at_ms": _ts(360),
                },
            ],
            "speech_events": [],
            "votes": [
                {"day": 1, "round": 1, "voter_seat": 1, "target_seat": 3, "submitted_at_ms": _ts(305)},
                {"day": 1, "round": 1, "voter_seat": 2, "target_seat": 3, "submitted_at_ms": _ts(310)},
                {"day": 1, "round": 1, "voter_seat": 3, "target_seat": 2, "submitted_at_ms": _ts(315)},
                {"day": 1, "round": 1, "voter_seat": 4, "target_seat": 3, "submitted_at_ms": _ts(320)},
                {"day": 1, "round": 1, "voter_seat": 5, "target_seat": 3, "submitted_at_ms": _ts(325)},
                {"day": 1, "round": 1, "voter_seat": 6, "target_seat": 3, "submitted_at_ms": _ts(330)},
                {"day": 1, "round": 1, "voter_seat": 7, "target_seat": 2, "submitted_at_ms": _ts(335)},
                {"day": 1, "round": 1, "voter_seat": 8, "target_seat": 2, "submitted_at_ms": _ts(340)},
                {"day": 1, "round": 1, "voter_seat": 9, "target_seat": 3, "submitted_at_ms": _ts(345)},
            ],
            "night_actions": [],
        },
        {
            "day": 1,
            "phase": "NIGHT",
            "started_at_ms": _ts(360),
            "public_logs": [
                {
                    "kind": "PHASE_CHANGE",
                    "actor_seat": None,
                    "text": "夜のフェイズへ移行致します。制限時間は 90 秒でございます。",
                    "created_at_ms": _ts(360),
                },
            ],
            "speech_events": [],
            "votes": [],
            "night_actions": [
                {"day": 1, "actor_seat": 2, "kind": "DIVINE", "target_seat": 7, "submitted_at_ms": _ts(380)},
                {"day": 1, "actor_seat": 6, "kind": "GUARD", "target_seat": 2, "submitted_at_ms": _ts(395)},
                {"day": 1, "actor_seat": 7, "kind": "ATTACK", "target_seat": 2, "submitted_at_ms": _ts(420)},
            ],
        },
        {
            "day": 2,
            "phase": "DAY_DISCUSSION",
            "started_at_ms": _ts(450),
            "public_logs": [
                {
                    "kind": "MORNING",
                    "actor_seat": None,
                    "text": "平和な朝です。昨晩の犠牲者はいません。",
                    "created_at_ms": _ts(450),
                },
                {
                    "kind": "PHASE_CHANGE",
                    "actor_seat": None,
                    "text": "2 日目の議論を開始致します。制限時間は 240 秒でございます。",
                    "created_at_ms": _ts(450),
                },
            ],
            "speech_events": [
                _speech(2, "text", _ts(470), "席7を占いました。黒判定です。席7 シゲミチが人狼です。", summary="seat 2 reveals seat 7 is wolf"),
                _speech(5, "text", _ts(490), "霊媒結果: 席3 ジーナは人狼でした。占い騙りで確定です。",
                        co_declaration="medium", summary="seat 5 confirms seat 3 was wolf"),
                _speech(6, "text", _ts(510), "騎士COします。昨夜は席2を護衛して、襲撃を弾きました。",
                        co_declaration="knight", summary="seat 6 reveals knight, guarded seat 2"),
                _speech(7, "text", _ts(530), "私は人狼じゃない。席2の占いは捏造です。", summary=None),
                _speech(8, "text", _ts(550), "席2 セツが本当に占い師なら、席7 黒は信じざるを得ない。", summary=None),
                _speech(1, "text", _ts(570), "占い・霊媒・騎士の3CO全部噛み合ってるので席7処刑で行きましょう。", summary=None),
            ],
            "votes": [],
            "night_actions": [],
        },
        {
            "day": 2,
            "phase": "DAY_VOTE",
            "started_at_ms": _ts(700),
            "public_logs": [
                {
                    "kind": "EXECUTION",
                    "actor_seat": 7,
                    "text": "席7 ⚔ シゲミチ が処刑されました。\n\n投票内訳: 席7=7票 / 席2=1票",
                    "created_at_ms": _ts(760),
                },
                {
                    "kind": "VICTORY",
                    "actor_seat": None,
                    "text": "村人陣営の勝利!",
                    "created_at_ms": _ts(760),
                },
                {
                    "kind": "ROLE_REVEAL",
                    "actor_seat": None,
                    "text": (
                        "役職公開:\n"
                        "席1 あなた = 村人 (生存)\n"
                        "席2 🌙 セツ = 占い師 (生存)\n"
                        "席3 🍷 ジーナ = 人狼 (1日目処刑)\n"
                        "席4 🎲 SQ = 村人 (生存)\n"
                        "席5 🔮 ラキオ = 霊媒師 (生存)\n"
                        "席6 🛡 ステラ = 騎士 (生存)\n"
                        "席7 ⚔ シゲミチ = 人狼 (2日目処刑)\n"
                        "席8 ☄ コメット = 狂人 (生存)\n"
                        "席9 📐 ジョナス = 村人 (生存)"
                    ),
                    "created_at_ms": _ts(760),
                },
            ],
            "speech_events": [],
            "votes": [
                {"day": 2, "round": 1, "voter_seat": 1, "target_seat": 7, "submitted_at_ms": _ts(710)},
                {"day": 2, "round": 1, "voter_seat": 2, "target_seat": 7, "submitted_at_ms": _ts(715)},
                {"day": 2, "round": 1, "voter_seat": 4, "target_seat": 7, "submitted_at_ms": _ts(720)},
                {"day": 2, "round": 1, "voter_seat": 5, "target_seat": 7, "submitted_at_ms": _ts(725)},
                {"day": 2, "round": 1, "voter_seat": 6, "target_seat": 7, "submitted_at_ms": _ts(730)},
                {"day": 2, "round": 1, "voter_seat": 7, "target_seat": 2, "submitted_at_ms": _ts(735)},
                {"day": 2, "round": 1, "voter_seat": 8, "target_seat": 7, "submitted_at_ms": _ts(740)},
                {"day": 2, "round": 1, "voter_seat": 9, "target_seat": 7, "submitted_at_ms": _ts(745)},
            ],
            "night_actions": [],
        },
        {
            "day": 2,
            "phase": "GAME_OVER",
            "started_at_ms": _ts(760),
            "public_logs": [],
            "speech_events": [],
            "votes": [],
            "night_actions": [],
        },
    ]


def _speech(
    seat_no: int,
    source: str,
    ts_ms: int,
    text: str,
    *,
    co_declaration: str | None = None,
    summary: str | None = None,
    stt_confidence: float | None = None,
    addressed_seat_no: int | None = None,
) -> dict[str, Any]:
    return {
        "event_id": f"sp_{seat_no}_{ts_ms}",
        "source": source,
        "speaker_seat": seat_no,
        "text": text,
        "stt_confidence": stt_confidence,
        "summary": summary,
        "co_declaration": co_declaration,
        "addressed_seat_no": addressed_seat_no,
        "created_at_ms": ts_ms,
    }


# ─── trace synthesis ──────────────────────────────────────────────────────
GAMEPLAY_MODEL = "grok-4-1-fast"
NPC_MODEL = "grok-4-1-fast"
VOICE_MODEL = "gemini-2.0-flash-lite"

ROLE_BY_SEAT = {s["seat_no"]: s["role"] for s in SEATS}
PERSONA_BY_SEAT = {s["seat_no"]: s["persona_key"] for s in SEATS}


def _gameplay_trace_entry(
    *,
    seat_no: int,
    phase: str,
    day: int,
    task: str,
    response_obj: dict[str, Any],
    user_prompt_extra: str,
    ts_offset_ms: int,
) -> dict[str, Any]:
    persona = PERSONA_BY_SEAT[seat_no]
    role = ROLE_BY_SEAT[seat_no]
    actor = f"seat={seat_no} persona={persona} role={role}"
    system_prompt = (
        "あなたは人狼ゲームのプレイヤーです。"
        f"あなたは席{seat_no}の役職 {role} として振る舞ってください。"
        "公開ログ、私的ログ、合法候補から、JSON 形式で行動を返してください。"
    )
    user_prompt = (
        f"現在のフェイズ: {phase} (day={day})\n\n"
        f"{user_prompt_extra}\n\n"
        "以下のいずれかの intent を返してください: speak / vote / night_action / skip"
    )
    response_str = json.dumps(response_obj, ensure_ascii=False)
    prompt_tokens = len(system_prompt) + len(user_prompt)
    completion_tokens = len(response_str)
    return {
        "ts": _iso_at(ts_offset_ms),
        "role": "gameplay",
        "provider": "xai",
        "model": GAMEPLAY_MODEL,
        "phase": phase,
        "day": day,
        "actor": actor,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "response": response_str,
        "latency_ms": random.randint(1100, 2400),
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
        },
        "error": None,
        "metadata": {"task": task},
    }


def _npc_trace_entry(
    *,
    seat_no: int,
    phase_id: str,
    day: int,
    response_obj: dict[str, Any],
    user_prompt_extra: str,
    ts_offset_ms: int,
) -> dict[str, Any]:
    persona = PERSONA_BY_SEAT[seat_no]
    actor = f"npc_id=npc_{persona} seat={seat_no} persona={persona}"
    system_prompt = (
        f"あなたはNPC '{persona}' として人狼ゲームに参加しています。"
        f"短いリアクティブ発話 (最大80字) を JSON で返してください。"
    )
    user_prompt = (
        f"現在のフェイズ ID: {phase_id}\n"
        f"あなたの席: 席{seat_no}\n\n"
        f"{user_prompt_extra}"
    )
    response_str = json.dumps(response_obj, ensure_ascii=False)
    prompt_tokens = len(system_prompt) + len(user_prompt)
    completion_tokens = len(response_str)
    return {
        "ts": _iso_at(ts_offset_ms),
        "role": "npc_speech",
        "provider": "openai-compat",
        "model": NPC_MODEL,
        "phase": phase_id,
        "day": day,
        "actor": actor,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "response": response_str,
        "latency_ms": random.randint(700, 1500),
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
        },
        "error": None,
        "metadata": {
            "request_id": f"req_{seat_no}_{ts_offset_ms}",
            "logic_packet_id": f"lp_{seat_no}_{ts_offset_ms}",
            "suggested_intent": "speak",
            "max_chars": 80,
        },
        "file_stem": f"npc_{persona}",
    }


def _voice_trace_entry(
    *,
    speaker_seat: int,
    phase_id: str,
    day: int,
    transcript: str,
    confidence: float,
    audio_bytes: int,
    ts_offset_ms: int,
    addressed_name: str | None = None,
    co_claim: str | None = None,
) -> dict[str, Any]:
    s = next(s for s in SEATS if s["seat_no"] == speaker_seat)
    actor = f"speaker_user_id={s['discord_user_id']} seat={speaker_seat} segment=seg_{ts_offset_ms}"
    system_prompt = (
        "あなたは人狼ゲームの音声ログ分析エンジンです。"
        "渡された音声(日本語)を書き起こし、JSON で返してください。"
    )
    user_prompt = f"[audio bytes={audio_bytes} mime=audio/wav]"
    response_obj = {
        "transcript": transcript,
        "summary": transcript[:30],
        "confidence": confidence,
        "co_claim": co_claim,
        "vote_target_seat": None,
        "stance": {},
        "addressed_name": addressed_name,
    }
    response_str = json.dumps(response_obj, ensure_ascii=False)
    return {
        "ts": _iso_at(ts_offset_ms),
        "role": "voice_stt",
        "provider": "gemini",
        "model": VOICE_MODEL,
        "phase": phase_id,
        "day": day,
        "actor": actor,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "response": response_str,
        "latency_ms": random.randint(800, 1600),
        "tokens": {
            "prompt": 250 + audio_bytes // 1024,
            "completion": len(response_str),
            "total": 250 + audio_bytes // 1024 + len(response_str),
        },
        "error": None,
        "metadata": {
            "audio_bytes": audio_bytes,
            "language": "ja-JP",
            "segment_id": f"seg_{ts_offset_ms}",
        },
        "file_stem": "voice_stt",
    }


def _iso_at(offset_ms: int) -> str:
    """Format CREATED_AT_MS + offset as ISO 8601 with Z suffix."""
    from datetime import UTC, datetime, timedelta
    dt = datetime(2026, 4, 28, 8, 0, 0, tzinfo=UTC) + timedelta(milliseconds=offset_ms)
    return dt.isoformat()


def build_trace() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    d1_disc = "g_sample_001::day1::DAY_DISCUSSION::1"
    d1_vote = "g_sample_001::day1::DAY_VOTE::1"
    d1_night = "g_sample_001::day1::NIGHT::1"
    d2_disc = "g_sample_001::day2::DAY_DISCUSSION::1"
    d2_vote = "g_sample_001::day2::DAY_VOTE::1"

    # Day 1 discussion: gameplay LLM speeches for each LLM seat
    speech_data_d1 = [
        (2, "占い師COします。初日ランダム白で席9 ジョナスを占いました。白判定です。", "seer"),
        (3, "私も占い師です。席4 SQを占って白でした。席2は何かおかしい。", "seer"),
        (5, "霊媒師COはまだしません。今日処刑が出てから結果を出します。", None),
        (9, "席2の白判定をもらった9番です。占い理由が筋が通ってる。", None),
        (4, "占いが2人いる。どちらが本物か議論したい。", None),
        (6, "占いCOの2人をしっかり見極めましょう。", None),
        (7, "席3が偽のように見える。占い理由が雑な印象。", None),
        (8, "私は席2が怪しいと感じる。席3を信じたい。", None),
    ]
    for i, (seat, text, co) in enumerate(speech_data_d1):
        entries.append(_gameplay_trace_entry(
            seat_no=seat,
            phase="DAY_DISCUSSION",
            day=1,
            task="discussion",
            user_prompt_extra=(
                "公開ログ抜粋:\n"
                "(SETUP) 参加者 9 名で人狼ゲームを開始します。\n"
                "(PHASE_CHANGE) 1 日目の議論を開始致します。\n"
                f"\nあなたの履歴: 初日議論ラウンド1, 席{seat}として発話"
            ),
            response_obj={
                "intent": "speak",
                "public_message": text,
                "target_name": None,
                "reason_summary": "1巡目の方針提示",
                "confidence": 0.7,
                "co_declaration": co,
            },
            ts_offset_ms=15_000 + i * 18_000,
        ))

    # Day 1 vote
    vote_data = [
        (2, 3, "占い理由が薄い席3を投票"),
        (3, 2, "対抗占い師の席2を投票"),
        (4, 3, "席3の論理が破綻気味"),
        (5, 3, "席3を吊って霊媒結果を取りたい"),
        (6, 3, "占いCO 2人だが席3の方が偽臭"),
        (7, 2, "席2が偽だと判断"),
        (8, 2, "席3を信じる立場"),
        (9, 3, "白を貰った相手が偽でないとすれば席3が偽"),
    ]
    for i, (voter, target, reason) in enumerate(vote_data):
        entries.append(_gameplay_trace_entry(
            seat_no=voter,
            phase="DAY_VOTE",
            day=1,
            task="vote",
            user_prompt_extra=(
                "投票先として合法な候補は: " + "、".join(_seat_token(s) for s in [2, 3, 4, 5, 6, 7, 8, 9] if s != voter)
                + "\n席1 あなたは合法候補に含まれません(あなた自身)"
            ),
            response_obj={
                "intent": "vote",
                "public_message": "",
                "target_name": _seat_token(target),
                "reason_summary": reason,
                "confidence": 0.85,
                "co_declaration": None,
            },
            ts_offset_ms=305_000 + i * 5_000,
        ))

    # Night 1 actions
    entries.append(_gameplay_trace_entry(
        seat_no=2,
        phase="NIGHT",
        day=1,
        task="night_action",
        user_prompt_extra="あなたは占い師。占い対象を 1 名選んでください。\n合法候補: 席4 SQ、席5 ラキオ、席6 ステラ、席7 シゲミチ、席8 コメット、席9 ジョナス",
        response_obj={
            "intent": "night_action",
            "public_message": "",
            "target_name": _seat_token(7),
            "reason_summary": "票筋的に席7が黒臭い、占って確定したい",
            "confidence": 0.75,
            "co_declaration": None,
        },
        ts_offset_ms=380_000,
    ))
    entries.append(_gameplay_trace_entry(
        seat_no=6,
        phase="NIGHT",
        day=1,
        task="night_action",
        user_prompt_extra="あなたは騎士。護衛対象を 1 名選んでください。\n合法候補: 席1 あなた、席2 セツ、席4 SQ、席5 ラキオ、席7 シゲミチ、席8 コメット、席9 ジョナス\n前夜護衛: なし",
        response_obj={
            "intent": "night_action",
            "public_message": "",
            "target_name": _seat_token(2),
            "reason_summary": "占い真目の席2を護衛するのが最有力",
            "confidence": 0.8,
            "co_declaration": None,
        },
        ts_offset_ms=395_000,
    ))
    entries.append(_gameplay_trace_entry(
        seat_no=7,
        phase="NIGHT",
        day=1,
        task="wolf_chat",
        user_prompt_extra="人狼チャット: 相方は席3だったが処刑された。襲撃先を選んでください。\n合法候補: 席1、席2、席4、席5、席6、席8、席9",
        response_obj={
            "intent": "speak",
            "public_message": "占い真の席2を噛むのが最優先。次点は霊媒候補の席5。",
            "target_name": None,
            "reason_summary": "占い噛みで情報遮断",
            "confidence": 0.7,
            "co_declaration": None,
        },
        ts_offset_ms=415_000,
    ))
    entries.append(_gameplay_trace_entry(
        seat_no=7,
        phase="NIGHT",
        day=1,
        task="night_action",
        user_prompt_extra="あなたは人狼。襲撃対象を 1 名選んでください。\n合法候補: 席1、席2、席4、席5、席6、席8、席9",
        response_obj={
            "intent": "night_action",
            "public_message": "",
            "target_name": _seat_token(2),
            "reason_summary": "占い噛みで進行を支配",
            "confidence": 0.75,
            "co_declaration": None,
        },
        ts_offset_ms=420_000,
    ))

    # Day 2 discussion: notable speeches
    speech_data_d2 = [
        (2, "席7を占いました。黒判定です。席7 シゲミチが人狼です。", None),
        (5, "霊媒結果: 席3 ジーナは人狼でした。占い騙りで確定です。", "medium"),
        (6, "騎士COします。昨夜は席2を護衛して、襲撃を弾きました。", "knight"),
        (7, "私は人狼じゃない。席2の占いは捏造です。", None),
        (8, "席2 セツが本当に占い師なら、席7 黒は信じざるを得ない。", None),
    ]
    for i, (seat, text, co) in enumerate(speech_data_d2):
        entries.append(_gameplay_trace_entry(
            seat_no=seat,
            phase="DAY_DISCUSSION",
            day=2,
            task="discussion",
            user_prompt_extra=(
                "公開ログ抜粋:\n"
                "(MORNING) 平和な朝です。昨晩の犠牲者はいません。\n"
                "(EXECUTION_PREV_DAY) 席3 ジーナ が処刑されました。\n"
                f"\nあなたの行動: 席{seat}として発話を準備"
            ),
            response_obj={
                "intent": "speak",
                "public_message": text,
                "target_name": None,
                "reason_summary": "情報整理して投票誘導",
                "confidence": 0.85,
                "co_declaration": co,
            },
            ts_offset_ms=470_000 + i * 20_000,
        ))

    # Day 2 vote: only key votes
    for i, (voter, target) in enumerate([(2, 7), (4, 7), (5, 7), (6, 7), (7, 2), (8, 7), (9, 7)]):
        entries.append(_gameplay_trace_entry(
            seat_no=voter,
            phase="DAY_VOTE",
            day=2,
            task="vote",
            user_prompt_extra="投票先として合法な候補は: 席2 🌙 セツ、席4 🎲 SQ、席5 🔮 ラキオ、席6 🛡 ステラ、席7 ⚔ シゲミチ、席8 ☄ コメット、席9 📐 ジョナス",
            response_obj={
                "intent": "vote",
                "public_message": "",
                "target_name": _seat_token(target),
                "reason_summary": "占い・霊媒・騎士の3CO一致から席7が確定" if target == 7 else "占い結果を疑う",
                "confidence": 0.95,
                "co_declaration": None,
            },
            ts_offset_ms=710_000 + i * 5_000,
        ))

    # Sample voice STT entries — pretend the human player (seat 1) spoke twice
    entries.append(_voice_trace_entry(
        speaker_seat=1,
        phase_id=d1_disc,
        day=1,
        transcript="席3の占い理由がパッとしない。席2の方が信用できそう。",
        confidence=0.91,
        audio_bytes=82_000,
        ts_offset_ms=170_000,
    ))
    entries.append(_voice_trace_entry(
        speaker_seat=1,
        phase_id=d2_disc,
        day=2,
        transcript="占い・霊媒・騎士の3CO全部噛み合ってるので席7処刑で行きましょう。",
        confidence=0.88,
        audio_bytes=110_000,
        ts_offset_ms=570_000,
    ))

    # A handful of NPC reactive speech entries (would be sent to NPC bots in reactive_voice mode)
    entries.append(_npc_trace_entry(
        seat_no=2,
        phase_id=d1_disc,
        day=1,
        user_prompt_extra=(
            "公開状況: 議論ラウンド1進行中。\n候補発話タイミング: あり。\n"
            "あなたの席=2 (セツ)。短い反応的発話を作ってください。"
        ),
        response_obj={
            "intent": "speak",
            "text": "占い師COします。初日ランダム白で席9 ジョナスを占いました。",
            "co_declaration": "seer",
            "used_logic_ids": ["lc_co_seer", "lc_white_jonas"],
        },
        ts_offset_ms=15_000,
    ))
    entries.append(_npc_trace_entry(
        seat_no=5,
        phase_id=d2_disc,
        day=2,
        user_prompt_extra="議論ラウンド進行中。霊媒結果を出すタイミング。",
        response_obj={
            "intent": "speak",
            "text": "霊媒結果: 席3 ジーナは人狼でした。占い騙りで確定です。",
            "co_declaration": "medium",
            "used_logic_ids": ["lc_medium_co", "lc_seat3_wolf"],
        },
        ts_offset_ms=490_000,
    ))

    return entries


def main() -> None:
    out = {
        "game": {
            "id": GAME_ID,
            "guild_id": GUILD_ID,
            "host_user_id": HOST_USER_ID,
            "discussion_mode": "rounds",
            "created_at_ms": CREATED_AT_MS,
            "ended_at_ms": DAY1_START_MS + 760_000,
            "victory": "village",
            "main_text_channel_id": "1498215837528621055",
            "main_vc_channel_id": "1498215837528621056",
        },
        "seats": [
            {
                **s,
                "alive": not (s["seat_no"] in (3, 7)),
                "death_cause": "EXECUTION" if s["seat_no"] in (3, 7) else None,
                "death_day": 1 if s["seat_no"] == 3 else (2 if s["seat_no"] == 7 else None),
            }
            for s in SEATS
        ],
        "phases": build_phases(),
        "trace": build_trace(),
    }
    target = Path(__file__).parent / "game-sample.json"
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {target.resolve()} ({target.stat().st_size:,} bytes)")
    print(f"  seats        = {len(out['seats'])}")
    print(f"  phases       = {len(out['phases'])}")
    print(f"  trace lines  = {len(out['trace'])}")


if __name__ == "__main__":
    main()
