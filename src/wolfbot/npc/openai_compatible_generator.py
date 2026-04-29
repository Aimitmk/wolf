"""Concrete NpcGenerator that calls any OpenAI-compatible chat-completions
endpoint for reactive speech.

Given a ``LogicPacket`` (summarised game state, logic candidates, pressure
map) and a ``SpeakRequest`` (max chars, phase, intent), this module builds
a minimal Japanese prompt and hits the configured chat-completions endpoint
with structured JSON output.

The provider is intentionally not baked into the class name. Swap it by
changing :class:`OpenAICompatibleConfig.base_url` and ``model``:

* xAI Grok — ``base_url="https://api.x.ai/v1"``, ``model="grok-..."``
* OpenAI — ``base_url="https://api.openai.com/v1"``, ``model="gpt-..."``
* Groq — ``base_url="https://api.groq.com/openai/v1"``
* Together AI — ``base_url="https://api.together.xyz/v1"``
* vLLM / Ollama (OpenAI-compatible mode) — local ``base_url``
* DeepSeek — ``mode="json_object"`` plus the JSON contract suffix appended
  to the system prompt; ``thinking`` / ``reasoning_effort`` forwarded via
  ``extra_body`` (DeepSeek does not support strict ``json_schema``).

The default is xAI Grok for back-compat with existing deployments. The
prompt is deliberately simpler than the full ``llm_service`` prompt
pipeline — reactive utterances are short (80-char cap) situational
remarks, not multi-paragraph analytical speeches. The persona's
``style_guide`` and ``speech_profile`` are included for voice consistency
but the strategic rules sections are omitted.

For Vertex AI Gemini, see :mod:`wolfbot.npc.gemini_generator` — that
provider is not OpenAI-compatible and uses the ``google-genai`` SDK.
The right generator for a given ``LLMDeciderConfig`` is picked by
:func:`wolfbot.npc.generator_factory.make_npc_generator`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

from wolfbot.domain.enums import CO_CLAIM_VALUES
from wolfbot.domain.ws_messages import LogicCandidate, LogicPacket, SpeakRequest
from wolfbot.llm.persona_base import Persona
from wolfbot.llm.prompt_builder import (
    build_judgment_profile_block,
    build_speech_profile_block,
)
from wolfbot.npc.personas import NPC_PERSONAS_BY_KEY
from wolfbot.npc.speech_service import NpcGeneratedSpeech

log = logging.getLogger(__name__)

_RESPONSE_SCHEMA: dict[str, object] = {
    "name": "reactive_speech",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "text",
            "intent",
            "used_logic_ids",
            "co_declaration",
            "addressed_seat_nos",
        ],
        "properties": {
            "text": {"type": "string", "maxLength": 300},
            "intent": {
                "type": "string",
                "enum": ["speak", "agree", "disagree", "question", "accuse", "defend", "skip"],
            },
            "used_logic_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "co_declaration": {
                "type": ["string", "null"],
                "enum": [*CO_CLAIM_VALUES, None],
            },
            "addressed_seat_nos": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": 9},
                "description": (
                    "Seat numbers this utterance is directed at. "
                    "Empty array `[]` for general remarks aimed at the whole "
                    "table. Single addressee → 1-element array (e.g. `[3]`); "
                    "asking multiple people in one breath → multi-element "
                    "(e.g. `[2, 3]` for 「セツとジナはどう?」). Master "
                    "prioritises every named seat in the next dispatch and "
                    "consumes them as each replies."
                ),
            },
        },
    },
}


def _build_system(
    persona: Persona,
    max_chars: int,
    *,
    role: str | None = None,
    role_strategy: str | None = None,
) -> str:
    """Build the NPC's system prompt.

    Mirrors rounds-mode `build_system_prompt` for the persona-shaping blocks
    (`build_speech_profile_block`, `build_judgment_profile_block`) so the
    reactive_voice NPC carries the same character data — `narration_mode`,
    `address_style`, `forbidden_overuse`, the 5 judgment axes, etc. —
    instead of the small subset the historical NPC prompt sent.

    `role` + `role_strategy` are optional: when Master sends them on the
    SpeakRequest, the NPC sees its role and the role-specific strategy
    block. Older Master builds that don't send them produce a prompt that
    silently omits the role section (back-compat).
    """
    role_block = ""
    if role:
        role_block = f"## あなたの役職\nあなたの役職は『{role}』です。役職に見える情報だけを根拠にしてください。\n\n"
    strategy_block = ""
    if role_strategy:
        strategy_block = f"## 役職別の戦術ヒント\n{role_strategy}\n\n"
    return (
        "あなたは人狼ゲームに参加中のプレイヤーです。\n"
        f"キャラクター名: {persona.display_name}\n"
        f"性格: {persona.style_guide}\n\n"
        f"## 話法\n{build_speech_profile_block(persona)}\n\n"
        f"## 判断のクセ\n{build_judgment_profile_block(persona)}\n\n"
        f"{role_block}"
        f"{strategy_block}"
        "## ルール\n"
        "- 日本語のみ。メタ発言禁止。AIであることに言及しない。\n"
        f"- `text` は {max_chars} 文字以内の短い発言。"
        f"上限ぎりぎりまで埋めようとせず、必ず文を最後まで言い切ること。"
        f"句読点や用言の途中で終わらないようにし、{max_chars} 文字に収めるためなら"
        "内容を削ってでも完結した文にする。\n"
        "- 発言しない場合は intent を `skip`、text を空文字にする。\n"
        "- `used_logic_ids` には参考にした logic candidate の id を入れる。\n"
        "- **`text` 内で人狼用語(メタ語彙)を使わない。** 内部の思考では使ってよいが、"
        "実際に喋る発話は素朴な日本語にする。\n"
        "  禁止例: 「CO」「占いCO」「霊媒CO」「騎士CO」「黒判定」「白判定」"
        "「ライン」「グレー」「グレラン」「縄」「PP」「ローラー」「破綻」「確白」「確黒」"
        "「鉄板護衛」「噛み筋」「票筋」「視点漏れ」「身内切り」「囲い」「相方」「2 人狼セット」など。\n"
        "  代わりに状況描写や感情で言う: "
        "「あの白判定、無理に庇ってる気がする」「昨夜守ったのは◯◯」"
        "「もう 1 人組んでそうな人」「あと処刑できる回数を考えると…」 のように。\n"
        "- **`text` 内で席番号 (席1, 席2, ..., 席9 や Seat3 等) を絶対に書かない。**"
        "プロンプトの `## 参加者` ブロックで席番号 → 名前のマッピングが冒頭に提示されており、"
        "それ以外のブロックは display_name のみで人物を参照している。"
        "あなたも他者を呼ぶときは必ず display_name (キャラ名) を使う。"
        "data 層 (`addressed_seat_nos` 等) には正しい席番号を入れて構わないが、"
        "発話そのものは「ジナさん」「ラキオ」のような自然な呼び方にする。"
        "状況整理を発話で行うときも『占いCO は ジョナスとsakura、霊媒CO はユリコとセツ』のように"
        "名前で並べる。`席4` `席1` のような数字並列は禁止。\n"
        "  禁止例: 「席3はどう思う?」「席4のラキオが…」「Seat 9、答えて」"
        "「席4と席1の占い主張、席9と席7の霊媒主張」\n"
        "  推奨例: 「ジョナスさんはどう思う?」「ラキオが…」「ユリコ、答えて」"
        "「ジョナスとsakuraの占い主張、ユリコとセツの霊媒主張」\n"
        "- 役職 CO (占い師・霊媒師・騎士として名乗る) をするときは、"
        "`co_declaration` を `\"seer\" / \"medium\" / \"knight\"` のいずれかに設定し、"
        "`text` は「実は私、占い師なんだ」など自然な名乗りにする。"
        "CO しないなら `co_declaration=null`。"
        "「占いCO」のような語そのものは `text` に書かない。\n"
        "- 特定の席に向けて話す場合は `addressed_seat_nos` にその席番号の配列を入れる。"
        "1人だけなら `[3]`、複数人に同時に問いかけるなら `[2, 3]` (例「セツとジナはどう?」)。"
        "誰宛でもない一般的な発言や全体への呼びかけは空配列 `[]`。"
        "自分の席を指定しても無効化されるので、相手の席を必ず入れること。"
        "`text` 中で名前を呼んだ全員ぶんを `addressed_seat_nos` に列挙する。"
        "Master は配列の全員を次に発話する優先候補として扱うので、"
        "問いかけた人数に応じて漏れなく入れる。\n"
        "- 死亡者リストには (処刑) または (襲撃) の死因タグが付く。"
        "前日の処刑死を「昨夜の犠牲者」と混同しない。逆も同様。"
        "発言で死を語るときはタグに合わせた表現を使う"
        "(例: 処刑死は「昨日処刑された」、襲撃死は「昨夜襲われた」)。\n"
    )


def _build_user(
    logic: LogicPacket,
    request: SpeakRequest,
    state: object | None = None,
) -> str:
    """Compose the speech LLM's user prompt.

    Naming policy: 席番号は冒頭の `## 参加者` ロスター 1 ブロックに
    集約し、それ以外の入力ブロックでは display_name のみで参照する。
    プロンプト全体に席番号が散らばっていると LLM が出力でも `席N` を
    引き写すドリフトが起きるため (game a3bbac5ca3e0 day5 で観測)、
    席番号は data 層 (`addressed_seat_nos` / `target_seat`) でだけ
    使う。
    """
    # Phase-D: prefer the bot's own NpcGameState mirror over the stale
    # SpeakRequest fields. The state carries role + alive/dead + private
    # results + wolf chat that the speech LLM needs to be in character.
    alive_seats = (
        getattr(state, "alive_seats", None)
        or list(request.alive_seats)
    )
    dead_seats = (
        getattr(state, "dead_seats", None)
        or list(request.dead_seats)
    )
    cause_map = (getattr(state, "dead_seat_causes", None) or {}) if state else {}

    def _cause_tag(seat_no: int) -> str:
        cause = cause_map.get(seat_no)
        if cause == "EXECUTION":
            return " (処刑)"
        if cause == "ATTACK":
            return " (襲撃)"
        return ""

    own_name: str | None = None
    for seat_no, name in alive_seats:
        if seat_no == request.seat_no:
            own_name = name
            break
    own_label = f"{own_name} (席{request.seat_no})" if own_name else f"席{request.seat_no}"

    lines = [
        f"フェイズ: {request.phase_id}",
        f"あなた: {own_label}",
        f"提案意図: {request.suggested_intent}",
    ]

    # Roster header — the ONLY block where seat numbers explicitly
    # appear. All other blocks below reference players by display_name.
    if alive_seats or dead_seats:
        lines.append("")
        lines.append("## 参加者 (席番号 → 名前)")
        if alive_seats:
            lines.append("生存中:")
            for seat_no, name in alive_seats:
                lines.append(f"  席{seat_no} {name}")
        if dead_seats:
            lines.append("死亡:")
            for seat_no, name in dead_seats:
                lines.append(f"  席{seat_no} {name}{_cause_tag(seat_no)}")

    lines.append("")
    lines.append("## 場の状況")
    lines.append(logic.public_state_summary or "(情報なし)")
    if logic.pending_role_callouts:
        callout_ja = {
            "seer": "占い師",
            "medium": "霊媒師",
            "knight": "騎士",
        }
        labels = "、".join(
            f"{callout_ja.get(c, c)} ({c})" for c in logic.pending_role_callouts
        )
        lines.append("")
        lines.append("## 未回答の役職呼びかけ")
        lines.append(
            f"次の役職に名乗り出が求められているがまだ誰も応答していない: {labels}。"
            "あなたが該当役職なら**直ちに CO する**ことを最優先で検討する。"
            "人狼/狂人なら、対抗 CO がまだ出ていない場合は**原則として騙りに出る**方を選ぶ。"
            "単独 CO の放置は『単独 CO = 真寄り』ルールにより真役職を村に真置きさせる結果になり、"
            "狼陣営の判定撹乱手段が失われる。"
            "潜伏を選ぶのは、相方が既に危険位置・CO 数が過密で誤爆/ローラー必至、等の"
            "具体的かつ強い理由があるときに限り、漠然とした『村が勝手に吊ってくれそう』で潜伏しない。"
        )
    # Private state — only present when Phase-D snapshot was received.
    if state is not None:
        partner_wolves = getattr(state, "partner_wolves", []) or []
        if partner_wolves:
            partners = "、".join(n for _s, n in partner_wolves)
            lines.append(f"## 仲間の人狼 (非公開)\n{partners}")
        seer_results = getattr(state, "seer_results", []) or []
        if seer_results:
            lines.append("## 自分の占い結果 (非公開)")
            for sr in seer_results:
                verdict = "黒 (人狼)" if sr.is_wolf else "白 (人狼ではない)"
                lines.append(
                    f"  day{sr.day}: {sr.target_name} → {verdict}"
                )
        medium_results = getattr(state, "medium_results", []) or []
        if medium_results:
            lines.append("## 自分の霊媒結果 (非公開)")
            for mr in medium_results:
                if mr.is_wolf is None:
                    verdict = "結果なし (処刑なし)"
                elif mr.is_wolf:
                    verdict = "人狼"
                else:
                    verdict = "人狼ではない"
                lines.append(
                    f"  day{mr.day}: {mr.target_name} → {verdict}"
                )
        guard_history = getattr(state, "guard_history", []) or []
        if guard_history:
            lines.append("## 自分の護衛履歴 (非公開)")
            for g in guard_history:
                outcome = (
                    "(平和な朝)" if g.peaceful_morning
                    else "(襲撃発生)" if g.peaceful_morning is False
                    else "(結果未確定)"
                )
                lines.append(
                    f"  day{g.day}: {g.target_name} を護衛 {outcome}"
                )
        wolf_chat_history = getattr(state, "wolf_chat_history", []) or []
        if wolf_chat_history:
            lines.append("## 人狼チャット履歴 (狼/狂人にのみ見える)")
            for wc in wolf_chat_history[-15:]:
                lines.append(
                    f"  day{wc.day} {wc.speaker_name}: {wc.text}"
                )
    if logic.past_votes:
        # Public vote history. Each NPC saw the EXECUTION public log when
        # it landed, but the per-phase fold doesn't carry that text into
        # the next day's prompt. Surfacing it here lets NPCs reason about
        # actual ballots ("ジナ → セツ") instead of fabricating their
        # own vote target.
        lines.append("")
        lines.append("## 公開された投票履歴")
        seat_name_lookup = {
            seat_no: name
            for seat_no, name in (
                list(alive_seats) + list(dead_seats)
            )
        }

        def _voter_label(seat: int | None) -> str:
            if seat is None:
                return "棄権"
            name = seat_name_lookup.get(seat)
            return name if name else "?"

        for day, round_, pairs in logic.past_votes:
            label = "決選投票" if round_ >= 1 else "投票"
            lines.append(f"- day{day} {label}:")
            for voter, target in pairs:
                lines.append(
                    f"    {_voter_label(voter)} → {_voter_label(target)}"
                )
    if logic.recent_speeches:
        lines.append("")
        lines.append("## 直近の発言 (古い順)")
        for sp in logic.recent_speeches:
            tag = _SOURCE_TAG.get(sp.source, sp.source)
            lines.append(f"- {sp.display_name} [{tag}]: {sp.text}")
    if logic.logic_candidates:
        lines.append("")
        lines.append("## 論点候補")
        for c in logic.logic_candidates:
            lines.append(_format_candidate(c))
    if logic.pressure:
        # MVP code paths leave this empty; rendered as name → score so
        # the seat-number column doesn't reappear here either.
        seat_name_lookup_p = {
            seat_no: name
            for seat_no, name in (
                list(alive_seats) + list(dead_seats)
            )
        }
        lines.append("")
        lines.append("## 圧力マップ (疑い度)")
        for seat, val in sorted(logic.pressure.items()):
            label = seat_name_lookup_p.get(seat) or f"席{seat}"
            lines.append(f"  {label}: {val:.2f}")
    lines.append("")
    lines.append(
        "上記を踏まえ、キャラクターとして自然な短い発言を生成してください。"
        "他者を呼ぶときは display_name (例: 「セツさん」「ラキオ」) を使い、"
        "発話文中で席番号 (席1 等) は絶対に書かない。"
        "席番号は data 層 (`addressed_seat_nos`) にだけ入れる。"
    )
    return "\n".join(lines)


# Friendly Japanese tags for the recent-speech source bracket. The NPC sees
# "[テキスト]" for typed messages, "[音声]" for STT output, "[NPC発話]" for
# other NPC bots — matches how human players naturally distinguish them.
_SOURCE_TAG: dict[str, str] = {
    "text": "テキスト",
    "voice_stt": "音声",
    "npc_generated": "NPC発話",
}


def _format_candidate(c: LogicCandidate) -> str:
    parts = [f"- [{c.id}] {c.claim}"]
    if c.support:
        parts.append(f"  根拠: {'、'.join(c.support)}")
    if c.counter:
        parts.append(f"  反論: {'、'.join(c.counter)}")
    return "\n".join(parts)


# DeepSeek does not support strict json_schema; it only supports
# json_object.  To make the model emit the right field names without
# walking the full schema in the system prompt, we append this per-call
# contract that mirrors the keys in ``_RESPONSE_SCHEMA``.  Module-level
# so tests can assert on substrings without instantiating AsyncOpenAI.
_DEEPSEEK_JSON_CONTRACT_SUFFIX = """\

---
出力形式 (json):
必ず次のキーを持つ JSON オブジェクトのみを返してください。前後にテキストや markdown コードフェンスを付けないでください。
- "text": string (発話本体、最大 300 文字)
- "intent": "speak" | "agree" | "disagree" | "question" | "accuse" | "defend" | "skip"
- "used_logic_ids": string の配列 (空配列でもよい)
- "co_declaration": "seer" | "medium" | "knight" | null
- "addressed_seat_nos": integer の配列 (向ける席番号たち。1人なら [3]、複数なら [2, 3]、誰宛でもない一般発言は [])

例:
{"text": "私もそこは引っかかってた。", "intent": "agree", "used_logic_ids": [], "co_declaration": null, "addressed_seat_nos": []}
{"text": "ジョナスさん、それは矛盾してるよ。", "intent": "accuse", "used_logic_ids": [], "co_declaration": null, "addressed_seat_nos": [3]}
{"text": "セツとジナ、ラキオの主張をどう見る?", "intent": "question", "used_logic_ids": [], "co_declaration": null, "addressed_seat_nos": [2, 3]}
"""


@dataclass
class OpenAICompatibleConfig:
    """Backend-agnostic config for any OpenAI Chat Completions endpoint.

    Defaults target xAI Grok for back-compat; override ``base_url`` and
    ``model`` to point at OpenAI, Groq, Together, vLLM, Ollama, etc.

    For DeepSeek, set ``mode="json_object"`` and (optionally) the
    DeepSeek-specific knobs ``thinking`` / ``reasoning_effort``.  Those
    values are no-ops in xAI / OpenAI / Groq / Together / Ollama mode.
    """

    model: str = "grok-4-1-fast"
    base_url: str = "https://api.x.ai/v1"
    timeout: float = 15.0
    temperature: float = 0.8
    # ``json_schema`` (default) sends ``response_format={"type":"json_schema",
    # "json_schema": _RESPONSE_SCHEMA}`` for strict structured output (xAI,
    # OpenAI). ``json_object`` falls back to ``{"type":"json_object"}`` and
    # appends ``_DEEPSEEK_JSON_CONTRACT_SUFFIX`` to the system prompt.
    mode: Literal["json_schema", "json_object"] = "json_schema"
    # DeepSeek-only knobs.  Forwarded via ``extra_body`` only when
    # ``mode == "json_object"``.
    thinking: Literal["enabled", "disabled"] = "enabled"
    reasoning_effort: Literal["high", "max"] = "max"


class OpenAICompatibleNpcGenerator:
    """Production NpcGenerator backed by any OpenAI-compatible LLM endpoint.

    Implements :class:`wolfbot.npc.speech_service.NpcGenerator` via the
    ``openai`` SDK's ``chat.completions`` API. The choice of provider is
    a config decision (``base_url`` + ``model`` + ``mode``), not a code
    decision.  Strict ``json_schema`` mode is the default; DeepSeek
    requires ``mode="json_object"`` plus the appended JSON contract.
    """

    def __init__(
        self,
        *,
        api_key: str,
        config: OpenAICompatibleConfig | None = None,
    ) -> None:
        self._api_key = api_key
        self.config = config or OpenAICompatibleConfig()
        self._persona_key: str | None = None

    def set_persona(self, persona_key: str) -> None:
        """Set the persona key for this NPC. Must be called once at startup,
        before any ``generate()`` invocation.  Raises if the key is unknown.
        """
        if persona_key not in NPC_PERSONAS_BY_KEY:
            valid = ", ".join(sorted(NPC_PERSONAS_BY_KEY.keys()))
            raise ValueError(
                f"unknown persona_key {persona_key!r}; valid keys: {valid}"
            )
        self._persona_key = persona_key

    async def generate(
        self,
        *,
        logic: LogicPacket,
        request: SpeakRequest,
        state: object | None = None,
    ) -> NpcGeneratedSpeech | None:
        from openai import AsyncOpenAI

        from wolfbot.services.llm_trace import (
            CallTimer,
            extract_openai_tokens,
            log_llm_call,
            parse_day_from_phase_id,
            parse_game_id_from_phase_id,
            trace_context,
        )

        if self._persona_key is None:
            raise RuntimeError(
                "OpenAICompatibleNpcGenerator.generate() called before set_persona(); "
                "each NPC bot must declare its persona at startup."
            )
        persona = NPC_PERSONAS_BY_KEY[self._persona_key]
        # Phase-D: prefer state.role over request.role; SpeakRequest's
        # role field is now a fallback for back-compat with older Master
        # builds that haven't started sending PrivateStateSnapshot.
        role_value = getattr(state, "role", None) or request.role
        system = _build_system(
            persona,
            max_chars=request.max_chars,
            role=role_value,
            role_strategy=request.role_strategy,
        )
        if self.config.mode == "json_object":
            system += _DEEPSEEK_JSON_CONTRACT_SUFFIX
        user = _build_user(logic, request, state)

        client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self.config.base_url,
        )
        kwargs: dict[str, object] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "timeout": self.config.timeout,
        }
        if self.config.mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": _RESPONSE_SCHEMA,
            }
        else:
            kwargs["response_format"] = {"type": "json_object"}
            kwargs["extra_body"] = {"thinking": {"type": self.config.thinking}}
            if self.config.thinking == "enabled":
                kwargs["reasoning_effort"] = self.config.reasoning_effort

        provider_tag = "deepseek" if self.config.mode == "json_object" else "openai-compat"
        actor = (
            f"npc_id={request.npc_id} seat={request.seat_no} persona={self._persona_key}"
        )
        timer = CallTimer()
        content = ""
        err: str | None = None
        tokens: dict[str, int | None] | None = None
        with trace_context(
            game_id=parse_game_id_from_phase_id(request.phase_id),
            phase=request.phase_id,
            day=parse_day_from_phase_id(request.phase_id),
            actor=actor,
            metadata={
                "request_id": request.request_id,
                "logic_packet_id": request.logic_packet_id,
                "suggested_intent": request.suggested_intent,
                "max_chars": request.max_chars,
                "base_url": self.config.base_url,
            },
        ):
            try:
                resp = await client.chat.completions.create(**kwargs)  # type: ignore[call-overload]
                content = resp.choices[0].message.content or "{}"
                tokens = extract_openai_tokens(resp)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                log.exception(
                    "npc_generate_failed model=%s base_url=%s",
                    self.config.model, self.config.base_url,
                )
                await log_llm_call(
                    role="npc_speech",
                    provider=provider_tag,
                    model=self.config.model,
                    system_prompt=system,
                    user_prompt=user,
                    response=None,
                    latency_ms=timer.elapsed_ms,
                    error=err,
                    file_stem=f"npc_{self._persona_key}",
                )
                return None

            await log_llm_call(
                role="npc_speech",
                provider=provider_tag,
                model=self.config.model,
                system_prompt=system,
                user_prompt=user,
                response=content,
                latency_ms=timer.elapsed_ms,
                error=None,
                tokens=tokens,
                file_stem=f"npc_{self._persona_key}",
            )

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            log.warning("npc_generate_invalid_json response=%s", content[:200])
            return None

        return _build_speech_from_json(data)


    async def decide_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, object],
    ) -> str:
        """Phase-D: structured-output decision call (vote / night action).

        Reuses this generator's provider config + auth so each persona's
        `NPC_LLM_*` doubles as both speech and decision backend without
        plumbing a separate client. The caller is `npc/decision_service.py`
        — it builds the prompt and validates the parsed result.

        Returns raw response text (a JSON string). On any provider error
        the exception propagates up so the dispatcher can record a
        timeout / abstain.
        """
        from openai import AsyncOpenAI

        from wolfbot.services.llm_trace import (
            CallTimer,
            extract_openai_tokens,
            log_llm_call,
        )

        client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self.config.base_url,
        )
        kwargs: dict[str, object] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
            "timeout": self.config.timeout,
        }
        if self.config.mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "decision",
                    "strict": True,
                    "schema": schema,
                },
            }
        else:
            kwargs["response_format"] = {"type": "json_object"}
            kwargs["extra_body"] = {"thinking": {"type": self.config.thinking}}
            if self.config.thinking == "enabled":
                kwargs["reasoning_effort"] = self.config.reasoning_effort

        provider_tag = "deepseek" if self.config.mode == "json_object" else "openai-compat"
        timer = CallTimer()
        content = ""
        err: str | None = None
        tokens: dict[str, int | None] | None = None
        try:
            resp = await client.chat.completions.create(**kwargs)  # type: ignore[call-overload]
            content = resp.choices[0].message.content or "{}"
            tokens = extract_openai_tokens(resp)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            log.exception(
                "npc_decide_failed model=%s base_url=%s",
                self.config.model, self.config.base_url,
            )
            await log_llm_call(
                role="npc_decision",
                provider=provider_tag,
                model=self.config.model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response=None,
                latency_ms=timer.elapsed_ms,
                error=err,
                file_stem=f"npc_{self._persona_key}",
            )
            raise

        await log_llm_call(
            role="npc_decision",
            provider=provider_tag,
            model=self.config.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response=content,
            latency_ms=timer.elapsed_ms,
            error=None,
            tokens=tokens,
            file_stem=f"npc_{self._persona_key}",
        )
        return content


def _build_speech_from_json(data: dict[str, object]) -> NpcGeneratedSpeech | None:
    """Map a parsed structured-output dict to ``NpcGeneratedSpeech``.

    Shared by every NPC generator (OpenAI-compat / DeepSeek / Vertex
    Gemini) so the schema-to-domain projection is validated in one
    place.  Returns ``None`` when the speech should be declined (empty
    text or ``intent="skip"``).
    """
    text = str(data.get("text", "") or "").strip()
    intent = str(data.get("intent", "speak"))
    if intent == "skip" or not text:
        return None

    raw_ids = data.get("used_logic_ids") or []
    used_ids = (
        tuple(str(x) for x in raw_ids) if isinstance(raw_ids, list) else ()
    )
    co_raw = data.get("co_declaration")
    co_declaration = co_raw if co_raw in CO_CLAIM_VALUES else None
    # `addressed_seat_nos` (list) is the authoritative field; the legacy
    # `addressed_seat_no` (singular) stays accepted for back-compat with
    # provider responses produced before 2026-04 multi-address rollout.
    # Coerce to int and silently drop non-int garbage rather than fail
    # the speech.
    raw_nos = data.get("addressed_seat_nos")
    addressed_seat_nos: list[int] = []
    if isinstance(raw_nos, list):
        for v in raw_nos:
            if (
                isinstance(v, int)
                and not isinstance(v, bool)
                and v not in addressed_seat_nos
            ):
                addressed_seat_nos.append(v)
    raw_addr = data.get("addressed_seat_no")
    addressed_seat_no: int | None = None
    if isinstance(raw_addr, int) and not isinstance(raw_addr, bool):
        addressed_seat_no = raw_addr
        if not addressed_seat_nos:
            addressed_seat_nos.append(raw_addr)
    elif addressed_seat_nos:
        addressed_seat_no = addressed_seat_nos[0]
    # Rough estimate: ~150ms per character for TTS
    estimated_ms = max(500, len(text) * 150)

    return NpcGeneratedSpeech(
        text=text,
        intent=intent,
        used_logic_ids=used_ids,
        estimated_duration_ms=estimated_ms,
        co_declaration=co_declaration,
        addressed_seat_no=addressed_seat_no,
        addressed_seat_nos=tuple(addressed_seat_nos),
    )


__all__ = [
    "_RESPONSE_SCHEMA",
    "OpenAICompatibleConfig",
    "OpenAICompatibleNpcGenerator",
    "_build_speech_from_json",
    "_build_system",
    "_build_user",
]
