"""Offline mock NPC speech generator used when ``NPC_LLM_PROVIDER=mock``.

Returns scripted utterances per persona, no network call.

Each NPC bot worker is bound to exactly one persona at startup. The factory
:func:`wolfbot.npc.generator_factory.make_npc_generator` calls
:meth:`MockNpcGenerator.set_persona` after construction so the mock can
pick the right canned-phrase pool. When the persona key is unknown to
this module, a generic fallback script is used so a new persona doesn't
break offline tests.

Designed so the full Master + NPC reactive_voice pipeline (WS server,
SpeakArbiter, VOICEVOX TTS, Discord VC playback) can be exercised end-
to-end without any LLM API access — the user hears actual NPC voice in
their VC, just with deterministic content.
"""

from __future__ import annotations

from collections.abc import Sequence

from wolfbot.domain.ws_messages import LogicPacket, SpeakRequest
from wolfbot.npc.speech_service import NpcGeneratedSpeech

_PERSONA_SCRIPTS: dict[str, tuple[str, ...]] = {
    "setsu": (
        "おはようございます。今日も慎重に進めましょう。",
        "席3さんの発言、少し気になりますね。",
        "もう少し情報が出てから判断したいです。",
    ),
    "gina": (
        "やっほー、皆元気にしてる?",
        "うーん、誰が怪しいのかなあ。",
        "まだ初日だから慌てなくていいよ。",
    ),
    "sq": (
        "発言の偏りを整理します。",
        "現時点で確定情報は少ないです。",
        "票筋を確認していきましょう。",
    ),
    "raqio": (
        "論理的に考えれば、まだ確定はできない。",
        "矛盾点を一つずつ潰そう。",
        "発言の前提条件を確認したい。",
    ),
    "stella": (
        "私は皆を信じたいけど、慎重にね。",
        "占い師さんの結果を待ちましょう。",
        "今夜は襲撃が怖いですね。",
    ),
    "shigemichi": (
        "よっしゃ、いっちょやったろか。",
        "怪しい奴は俺がぶっ飛ばす。",
        "村のためにもっと声出していこうぜ。",
    ),
    "chipie": (
        "ふふ、面白くなってきたね。",
        "私は静かに見ているわ。",
        "誰が嘘をついてるかしら。",
    ),
    "comet": (
        "わくわくしてきた!",
        "誰が人狼でも面白いね。",
        "情報を集めていこう。",
    ),
    "jonas": (
        "私の見立てでは、まだ動くべきではない。",
        "信用すべきは行動だ。",
        "焦らず観察しよう。",
    ),
    "kukrushka": (
        "(無言で頷く)",
        "(首を傾げる)",
        "(指で席を指し示す)",
    ),
    "otome": (
        "あら、緊張するわね。",
        "皆さん、落ち着いて議論しましょう。",
        "私は皆の味方ですよ。",
    ),
    "sha_ming": (
        "数字で見れば白要素が多い。",
        "確率論的に占い師は本物だろう。",
        "情報の出し方が鍵だ。",
    ),
    "remnan": (
        "わたしは記憶にあるパターンを照合中。",
        "過去の試合と類似点が多い。",
        "結論を急がないで。",
    ),
    "yuriko": (
        "皆の意見をまとめましょう。",
        "私が信じるのは行動だけです。",
        "今夜は誰を守るべきか。",
    ),
}

_FALLBACK_SCRIPT: tuple[str, ...] = (
    "そうですね、もう少し様子を見たいです。",
    "皆さんの意見を聞かせてください。",
    "今のところ判断は保留です。",
)


class MockNpcGenerator:
    """Round-robin scripted NpcGenerator for offline tests.

    Implements the implicit ``set_persona`` + :meth:`generate` contract
    used by ``make_npc_generator`` so it slots into the existing factory
    branch without further wiring.
    """

    def __init__(self, *, scripts: dict[str, Sequence[str]] | None = None) -> None:
        self._scripts: dict[str, tuple[str, ...]] = (
            {k: tuple(v) for k, v in scripts.items()}
            if scripts is not None
            else {k: v for k, v in _PERSONA_SCRIPTS.items()}
        )
        self._persona_key: str | None = None
        self._idx = 0
        self.call_count = 0

    def set_persona(self, persona_key: str) -> None:
        self._persona_key = persona_key
        self._idx = 0

    def _active_script(self) -> tuple[str, ...]:
        if self._persona_key is None:
            return _FALLBACK_SCRIPT
        return self._scripts.get(self._persona_key, _FALLBACK_SCRIPT)

    async def generate(
        self,
        *,
        logic: LogicPacket,
        request: SpeakRequest,
        state: object | None = None,
    ) -> NpcGeneratedSpeech | None:
        self.call_count += 1
        script = self._active_script()
        text = script[self._idx % len(script)]
        self._idx += 1
        if len(text) > request.max_chars:
            text = text[: request.max_chars]
        return NpcGeneratedSpeech(
            text=text,
            intent="speak",
            used_logic_ids=(),
            estimated_duration_ms=2000,
        )


__all__ = ["MockNpcGenerator"]
