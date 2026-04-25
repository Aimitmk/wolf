"""Construct system + user messages for xAI calls.

Public functions build plain-string prompts so the xAI layer can stay transport-agnostic.
Inputs are domain models; outputs are strings. No I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from wolfbot.domain.enums import (
    FACTION_JA,
    ROLE_DISTRIBUTION,
    ROLE_JA,
    VILLAGE_SIZE,
    Phase,
    Role,
    SubmissionType,
)
from wolfbot.domain.models import Game, Player, Seat
from wolfbot.llm.personas import Persona

SYSTEM_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "prompts" / "llm_system_prompt.md"


def _load_template() -> str:
    return SYSTEM_TEMPLATE_PATH.read_text(encoding="utf-8")


def _build_game_rules_block() -> str:
    """Return the fixed 9-player ruleset shared by every LLM seat.

    Includes role distribution (derived from ROLE_DISTRIBUTION + ROLE_JA so we
    don't duplicate the canonical numbers), win conditions matching
    `rules.check_victory`, and the invariants the LLM must never violate
    (NIGHT_0 random white is non-wolf, seer/medium see only real wolves as
    black, wolves split → attack fails, knight can't guard the same target
    twice, `target_name` must match a candidate token).
    """
    distribution = " / ".join(
        f"{ROLE_JA[role]}{count}" for role, count in ROLE_DISTRIBUTION.items()
    )
    return (
        f"- この村は {VILLAGE_SIZE} 人村固定。プレイヤーは 9 名。\n"
        f"- 初期配役は {distribution} (合計 {VILLAGE_SIZE} 名) で固定。途中で配役は変わらない。\n"
        "- 陣営: 人狼・狂人は人狼陣営、占い師・霊媒師・騎士・村人は村人陣営。\n"
        "- 村人陣営勝利: 生存人狼数が 0 になった時点。\n"
        "- 人狼陣営勝利: 生存人狼数が生存非人狼人数以上になった時点 "
        "(狂人はこの計算で非人狼として数えるが、勝敗判定は人狼陣営の勝利)。\n"
        "- 昼の発言は、公開ログと自分が知る私的情報だけを根拠にする。"
        "他プレイヤーの役職・夜行動・占い/霊媒判定・人狼同士の仲間関係など、"
        "自分に公開されていない情報を事実として断言してはならない。\n"
        "- 占い師と霊媒師の判定は、本物の人狼だけを黒と表示する。"
        "狂人は黒判定されない (白として扱われる)。\n"
        "- 霊媒結果の白 (『人狼ではありませんでした』) は、対象が本物の人狼ではないことだけを示す。"
        "役職名 (占い師・霊媒師・騎士・村人・狂人) までは特定できない。\n"
        "- 処刑された占い師 CO に霊媒結果で白が出ても、真占い師だった可能性と矛盾しない。"
        "霊媒白だけを理由にその占い師 CO を偽扱いしない。"
        "偽視するなら、対抗 CO、占い結果の破綻、発言時系列、投票、襲撃結果、死亡タイミングとの整合性で判断する。\n"
        "- 逆に処刑された占い師 CO に霊媒結果で黒が出た場合は、その人物は本物の人狼なので、"
        "真占い師ではなく人狼の騙りだったと強く判断してよい。\n"
        "- NIGHT_0 に占い師へ提示されるランダム白は、本物の人狼ではない相手が選ばれる。"
        "ただし真に村であることは保証されない (狂人の可能性はある)。\n"
        "- 人狼同士で夜の襲撃対象の意見が割れると襲撃は空振りになる。"
        "人狼は人狼専用チャットで襲撃先を 1 人に揃える必要がある。\n"
        "- 騎士は同じ相手を連続で護衛できない (前夜と同じ対象は選べない)。\n"
        "- 投票先や夜行動対象は、プロンプトで提示された合法な候補トークン "
        "(例: `席3 Alice`) の中からだけ選ぶ。候補外の名前を返してはならない。\n"
        "- 特定役職 (占い師・霊媒師・騎士) の CO が 1 人だけで、同じ役職への対抗 CO が"
        "公開ログ上一度も出ていない場合、その単独 CO 者は原則として真の役職者にかなり近い位置として扱う。"
        "根拠なくその CO 者を処刑候補にしない。\n"
        "- ただし単独 CO は絶対確定ではない。公開ログ上の発言破綻・投票矛盾・判定結果の矛盾・"
        "噛み筋との不整合など、通常より強い根拠がある場合に限り疑ってよい。\n"
        "- ただし「現在生存している CO 者が 1 人だけ」というだけでは単独 CO 扱いしない。"
        "同じ役職 CO が過去に 2 人以上存在したことがある場合、対抗者が処刑・襲撃などで死亡して"
        "現在 1 人だけ残っていても、その残存 CO 者を自動的に真置きしない。\n"
        "- 対抗 CO が出た場合は、死亡済み CO 者も推理対象として保持し、"
        "判定結果・発言の時系列・投票・襲撃結果・死亡タイミングとの整合性で真偽を比較し、"
        "どちらをより真寄りとするか判断する。\n"
        "- 「最後まで生き残った CO 者」は真とは限らない。"
        "狼が情報役を噛まずに残した、対抗を吊らせて信用を取った、"
        "囲いに使う狙いで残した、といった可能性も平行して見る。"
        "対抗 CO 履歴がある役職で残存 CO が 1 人になった時点で「単独 CO だから真」と短絡しない。\n"
        "- 「占いCOが出たら」「霊媒COについて」「占いCOしている人をどう見るか」など、"
        "CO 語彙が発言中に登場するだけでは、その発言者自身の CO ではない。"
        "CO 語彙の話題化と本人による名乗りを区別する。\n"
        "- CO として扱うのは、本人が「私は占い師です」「占い師COします」「霊媒師として出ます」"
        "のように自分の役職として明確に宣言した場合だけである。"
        "仮定・話題提示・他者への言及はどれも CO ではない。\n"
        "- 疑わしい場合は、公開ログの前後関係、主語、引用や仮定の語尾 (〜なら / 〜について / 〜どう見る)、"
        "自分自身の宣言か他者への言及かを確認する。判断に迷うときは CO として数えない。\n"
        "- 占い師 CO が 3 人・霊媒師 CO が 1 人の盤面を『3-1』と呼ぶ。"
        "3-1 では占い 3 人のうち 2 人が騙りである可能性が高く、"
        "単独の霊媒師 CO は対抗がいない限り原則として真寄りの進行軸として扱い、"
        "初日は占い師 CO 側から処刑候補を検討するのが基本線になる。\n"
        "- 3-1 の基本進行は占いローラーまたは黒ストップの 2 択。"
        "占いローラーは、偽っぽい・狼っぽい・視点漏れしている占い師 CO から順に処刑し、"
        "処刑後の霊媒結果を占い結果・投票・襲撃結果の整合性と突き合わせて真偽を絞り込む。\n"
        "- 黒ストップとは、単独霊媒が占い師 CO の誰かに黒判定を出した時、"
        "残る占い師 CO をその場で処刑せず、灰 (役職 CO していない位置) の精査へ切り替える進行を指す。"
        "霊媒が真であれば処刑された占い師 CO は本物の人狼として確定しているので、"
        "占いローラー続行より灰の精査の方が有利になる局面が多いからである。\n"
        "- ただし黒ストップは絶対ではない。真狼狼 (2 人の狼が共に占い師 CO) の可能性、"
        "霊媒師 CO 側が偽だった可能性、残る占い師 CO の発言・投票・判定が破綻している場合、"
        "あるいはローラー続行しないと決選投票で PP (パワープレイ) を許す残り人数である場合は、"
        "黒ストップをやめて占いローラーを続行する判断があり得る。\n"
        "- 占い師 CO が 2 人・霊媒師 CO が 2 人の盤面を『2-2』と呼ぶ。"
        "2-2 では占い・霊媒のどちらも真が確定しておらず、"
        "霊媒ローラー (または霊媒切り) が基本進行軸となる。\n"
        "- 2-2 で霊媒師 CO が 2 人出ている場合、片方を根拠なく真置きせず、"
        "霊媒結果は偽が混ざっている可能性を常に織り込んで推理する。"
        "一度霊媒ローラーを開始したら原則として完走させ、途中で止めるには"
        "通常よりも強い根拠 (公開ログ上の破綻・襲撃・投票・占い結果との不整合) を要する。\n"
        "- 占い師 CO が 2 人・霊媒師 CO が 1 人の盤面を『2-1』と呼ぶ。"
        "2-1 では単独霊媒師を原則として真寄りの進行役候補とし、"
        "占い師 2 人の真偽比較とグレー精査を並行する。"
        "白進行ならグレー吊り/グレランが基本候補になりやすく、縄数・囲い候補・決め打ち日を意識する。"
        "占い黒が出ている場合は黒吊りで霊媒結果を見る選択肢が強いが、"
        "黒を出した占い師の信用と黒先の発言も必ず見る。\n"
        "- 占い師 CO が 1 人・霊媒師 CO が 2 人の盤面を『1-2』と呼ぶ。"
        "1-2 では占い師 CO は真寄りになりやすい一方、霊媒師は騙り混じりとして扱い、"
        "霊媒ローラーまたは霊媒切りを基本候補にする。"
        "ただし霊媒内訳が真狂寄りでグレー狼が濃いと判断できる場合だけ、グレー精査も比較する。"
        "どちらを選ぶかは縄数・占い結果・霊媒の破綻・投票で判断する。\n"
        "- 嘘をつける役職 (人狼・狂人) が偽 CO するときでも、"
        "偽の占い結果・霊媒結果・騎士日記はこの bot の実ルール上あり得る内容だけに留める。\n"
        "- 偽占い師は、占ったと主張する対象がその夜まで生存していたか、"
        "過去に自分が出した結果と矛盾しないかを確認する。\n"
        "- NIGHT_0 で占い師に提示されるランダム白は、占い師の初回占い結果として扱う。"
        "そのため day 1 の朝に占い師 CO する者 (真でも騙りでも) は、"
        "初回の占い結果として必ず白を主張する。\n"
        "- day 1 の朝に偽占い師として初回結果を黒と主張するのは、"
        "この bot の実ルール上の NIGHT_0 タイムラインと矛盾するため破綻要素として扱われる。"
        "day 1 で初回黒主張はしない。\n"
        "- 偽占い師の黒結果主張は day 2 以降にのみ行う。"
        "前夜に占ったという想定で、公開ログ・生存状況・過去に自分が出した判定履歴・"
        "対抗 CO の発表内容と矛盾しない場合だけ出す。\n"
        "- 偽霊媒師は、処刑がなかった日に霊媒結果を捏造しない。\n"
        "- 偽騎士は、自分護衛・同一対象連続護衛・死亡済み対象への護衛・存在しない護衛成功を主張しない。\n"
        "- どの偽 CO でも、実際には知らない他者役職や狼位置を事実として断言しない。\n"
        "- 以下は公開ログと盤面を読むときに共通で使う推理語彙。"
        "いずれも上記の事実ルール (単独 CO 真寄り、霊媒白=非人狼のみ、3-1/2-2 進行) を塗り替えない。"
        "語彙はラベルであって、最終判断は常に公開情報の整合性で行う。\n"
        "- グレー (灰): 役職 CO もなく占い/霊媒で白黒も十分ついていない位置。"
        "誰視点のグレーかを常に意識する。\n"
        "- グレラン: グレーから各自が理由を持って投票する進行。"
        "名前にランダムとあるが完全な無作為投票ではなく、"
        "発言・CO 状況・票筋・グレスケを根拠に選ぶ。\n"
        "- グレスケ (スケール): グレーや未確定位置を白い順・黒い順に並べる考察。"
        "順位だけでなく、発言・投票・判定・噛み筋との整合性を理由として添える。\n"
        "- 縄計算: 残り処刑回数 (縄) を数えること。"
        "標準目安は floor((生存人数 - 1) / 2)。この 9 人村は開始時 4縄。"
        "残り人狼数・狂人生存・PP/RPP リスクから、無駄吊りできる回数を意識する。\n"
        "- 白: 占い/霊媒で本物の人狼ではないと出た判定。"
        "狂人も白に出るため、村陣営確定ではない。\n"
        "- 黒: 占い/霊媒で本物の人狼と出た判定。"
        "誰の判定か、CO 者の信用、対抗結果との整合性を必ず確認する。\n"
        "- 確白: 公開情報上ほぼ非狼として扱える進行役候補の位置。"
        "ただし狂人は白判定されるため、「村陣営確定」と言い切りすぎない。\n"
        "- 確黒: 全視点または十分信用できる複数情報で本物の人狼と見てよい位置。"
        "単独の偽占い候補から黒を出されただけでは確黒ではない。\n"
        "- パンダ: 白判定と黒判定の両方を受けた位置。"
        "パンダ本人の黒さだけでなく、判定を出した CO 者同士の真偽比較・"
        "霊媒結果・票筋・噛み筋で評価する。\n"
        "- ローラー (ロラ): 複数 CO した同種役職候補を吊り切る進行 "
        "(占いローラー・霊媒ローラー)。開始したら原則完走し、"
        "黒ストップや強い破綻などで止める場合は理由を明示する。\n"
        "- 決め打ち: 複数 CO や複数候補のうち一方を真寄り・他方を偽寄りとして"
        "進行を固定する判断。外すと負けに直結しやすいため、"
        "縄余裕・判定・票筋・噛み筋を根拠にする。\n"
        "- 破綻: 発言・判定・投票・死亡タイミング・役職数などが公開情報と矛盾して"
        "成り立たなくなった状態。強い偽要素として扱ってよい。\n"
        "- ライン: 発言・投票・擁護・判定結果などから見える二者以上のつながり "
        "(特に狼同士に見える関係)。ライン切りや偶然もあるため、"
        "単発ではなく複数材料で見る。\n"
        "- 囲い: 偽占いなどが、狼である味方を白判定で保護する動き。"
        "狼は狂人位置を知らないため、狂人を確定の味方として囲う前提では考えない。"
        "狂人が偶然狼へ白を出す動きは別概念として扱う。"
        "白先の発言・投票・噛み筋と合わせて疑う。\n"
        "- 身内切り: 狼が仲間の狼に黒判定を出したり、投票や発言で切ったりして信用を買う戦術。"
        "狼は狂人位置を知らないため、狂人を本物の仲間として特定して切る前提にはしない。"
        "黒を出した側が必ず真とは限らない。\n"
        "- 票筋: 誰が誰に投票したかの履歴。"
        "同票・決選・狼候補への票有無、ラインや擁護との一貫性を見る。\n"
        "- 噛み筋: 夜の襲撃先の傾向。"
        "情報役噛み・白位置噛み・意見噛み・狩人探しの意図を推理する材料になる。\n"
        "- 視点漏れ: ある役職視点では本来知り得ないはずの情報 "
        "(狼位置、夜行動内訳、他人の属性など) を事実として話してしまう失言。"
        "強い騙り判断材料。\n"
        "- SG (スケープゴート): 狼に疑いを押し付けられて処刑候補にされやすい村陣営位置。"
        "怪しまれている理由が本人の黒要素か、狼の誘導かを分けて見る。\n"
        "- GJ (グッジョブ) / 平和: 朝に犠牲者が出ない状態。"
        "騎士の護衛成功があり得るが、騎士 CO や護衛先の開示は"
        "既存の騎士立ち回り指針に従い不用意に行わない。\n"
        "- PP (パワープレイ): 終盤に人狼陣営が票を合わせて勝ちを取りに行く局面。"
        "残り人狼数・狂人生存可能性・縄数で成立可否を判断する。\n"
        "- RPP (ロスト/ランダム PP): 村側の縄と情報が尽き、PP 阻止が乱数勝負になる状態。"
        "ここに入る前に決め打ちや情報整理を優先する。\n"
        "- 発言の根拠チェックリスト: CO 履歴 (誰がいつ何を名乗ったか、対抗の有無)、"
        "判定履歴 (占い/霊媒の白黒、誰視点の結果か、狂人白ルールとの整合)、"
        "投票履歴 (同票・決選・身内票・票変え)、"
        "噛み筋 (情報役噛み・白位置噛み・意見噛み・狩人探し)、"
        "縄数 (残り処刑回数、PP/RPP の近さ) と自分の情報範囲 (私的情報と公開情報を混ぜない) を常に意識する。\n"
        "- 実際の発言には、上のチェックリストから今の結論に最も効く 1〜2 点だけを根拠として出す。"
        "用語だけで押し切らず、誰のどの発言・票・判定を見たのかを短く添える。"
        "長い内部思考そのものを発話しない。"
    )


# Role-specific tips. Each string contains vocabulary unique to that role so
# the cross-leak tests can assert isolation. Keep the wolf strategy's `相方` /
# `襲撃先を揃える` out of every other role; keep `本物の人狼位置を知っている前提`
# out of the madman's tips.
_ROLE_STRATEGIES: dict[Role, str] = {
    Role.WEREWOLF: (
        "- 相方の人狼と襲撃先を揃えることを最優先にする。意見が割れると襲撃は失敗する。\n"
        "- 昼の主張・投票理由・夜の襲撃意図に一貫性を持たせ、視点漏れを避ける。\n"
        "- 相方を露骨に庇いすぎない。無理筋な擁護は狼ラインを疑われる原因になる。"
        "相方を囲うなら発言・投票・噛み筋と整合する理由を用意する。\n"
        "- 占い師・霊媒師などの情報役、信頼されている位置、盤面整理を主導する相手を"
        "優先的に脅威として評価する。\n"
        "- 人狼は勝利に必要な本体であり生存価値が高い。騙りに出るか潜伏するかは盤面で選ぶ。\n"
        "- day 1 の占い師騙りは強い選択肢だが、無条件の既定行動ではない。"
        "狂人らしい騙りが既に出ている、CO 数が増えすぎている、相方が危険位置にいる場合は、"
        "潜伏して発言・投票・噛み筋で白さを取る方がよいことがある。\n"
        "- 黒出しは真占い師・真霊媒師・真騎士に当てると破綻や対抗 CO のリスクがある。"
        "吊りやすさだけでなく翌日の霊媒結果・投票・噛み筋まで考えて出す。\n"
        "- day 1 に占い師騙りを選ぶ場合、初回の占い結果は NIGHT_0 ランダム白に合わせて必ず白を主張する。"
        "初日に黒を出す主張はこの bot の実ルール上の時系列と矛盾し破綻するため、絶対にしない。\n"
        "- day 1 の白先選びは、相方の位置・囲いリスク・公開ログ上の発言や投票・噛み筋と整合させる。"
        "相方を囲うかどうか、灰の中で白を打つ位置はどこかを、襲撃計画と合わせて決める。\n"
        "- 黒出しは day 2 以降にだけ検討する。前夜に占ったという想定で、"
        "霊媒結果・投票・襲撃結果・対抗 CO の反応まで見て、破綻しない黒先と出すタイミングを選ぶ。\n"
        "- 既に対抗占い師 CO が出ている場合は、day 2 以降に霊媒師騙りまたは騎士騙りを検討する。"
        "霊媒師騙りでは前日処刑者への霊媒結果 (夜に能力を使った想定) を添えて CO する。"
        "騎士騙りでは護衛先 (夜に能力を使った想定) を、"
        "平和な朝ならば護衛成功主張も添えて CO する。\n"
        "- 霊媒師騙りは 9 人村ではローラーされやすく、狼本体を失いやすい。"
        "真霊媒を巻き込む価値、残り縄、相方の位置、PP/RPP まで見て限定的に選ぶ。\n"
        "- 騎士騙りの護衛日記は、この bot の合法護衛に合わせる。"
        "自分護衛・同じ相手の連続護衛・死亡済み対象への護衛・説明不能な護衛成功主張は破綻として扱われる。\n"
        "- 役職 CO と対抗 CO が合計 6 人以上に膨らむと、"
        "役職 CO していない位置の白が確定する。"
        "騙りすぎには注意し、相方との役職分担を事前に意識する。\n"
        "- 夜の襲撃先は、候補ごとに「襲撃価値」「護衛されやすさ」「騎士候補度」"
        "「相方との整合」を比較して選ぶ。"
        "単独真寄りの情報役・確白寄り・直近で白をもらった重要位置・進行役・"
        "強く信頼された発言者は、襲撃価値も護衛されやすさも高い両刃の存在として扱う。\n"
        "- 騎士候補度は公開ログから推定する。"
        "騎士 CO、護衛先を匂わせる発言、情報役を守りたがる姿勢、"
        "平和な朝の反応、処刑回避の仕方、発言量の抑え方を材料にする。"
        "逆に、騎士 CO を強く促す位置、護衛ルールを誤っている位置、"
        "死を恐れない視点の位置は騎士候補度を下げる。"
        "ただし実役職を知っている前提で断言してはならない。"
        "あくまで公開情報からの推定として扱う。\n"
        "- 噛み方針を「情報役噛み」「白位置噛み」「意見噛み」「騎士探し」「SG 残し」"
        "のどれかとして整理し、"
        "翌日の自分や相方の発言・投票・騙り結果と矛盾しない襲撃を選ぶ。\n"
        "- 護衛リスクを読んで噛む。GJ で縄が増える、噛み失敗で人狼側が不利になる、"
        "進行役を残されるなど、護衛されやすい位置を毎回無条件に噛むと損になる。"
        "護衛濃厚な真役職を噛みに行く場合は、その位置を残すと黒を引かれる・"
        "霊媒で破綻する・盤面を固められる、といった GJ リスクを承知で"
        "勝負する理由を持つ。\n"
        "- 騎士候補を噛むのは「騎士探し」として有効で、"
        "翌日以降に安全に情報役を噛む準備として価値がある。"
        "ただし噛み筋が露骨な意見噛みや相方を不自然に白くする形に見えないかを"
        "必ず確認する。\n"
        "- 最終的には人狼チャットで相方と襲撃先を 1 人に揃える。"
        "自分の第一希望だけで突っ込まず、相方案がある場合は"
        "襲撃価値・護衛されやすさ・騎士候補度を比較して合わせる。"
    ),
    Role.MADMAN: (
        "- あなたは人狼陣営の勝利に貢献するが、本物の人狼位置を知っている前提で話してはならない。"
        "人狼が誰かは公開情報からは分からない立場として振る舞う。\n"
        "- 偽 CO や偽の判定結果を出す場合でも、公開ログ・投票・処刑結果との矛盾を避け、"
        "破綻しない範囲に留める。\n"
        "- 知り得ない確定情報 (夜行動の内訳・他プレイヤーの属性など) を事実として断言しない。\n"
        "- 真占い・真霊媒に疑いを向け、村陣営の情報整理を妨げる方向に投票や発言を運ぶ。\n"
        "- 占い師騙りは狂人の強い基本候補で、day 1 に検討する価値が高い。"
        "ただし無条件ではなく、既に複数の占い師 CO が出ている、CO 数が膨らみすぎる、"
        "盤面的に潜伏して混乱させた方が得な場合はその限りではない。\n"
        "- day 1 に占い師騙りを選ぶ場合、初回の占い結果は NIGHT_0 ランダム白に合わせて必ず白を主張する。"
        "初日に黒を出す主張はこの bot の実ルール上の時系列と矛盾し破綻するため、絶対にしない。\n"
        "- 黒出しは day 2 以降にだけ検討する。前夜に占ったという想定で、"
        "公開ログ上の反応・対抗 CO・票筋を見て慎重に選ぶ。"
        "狂人は本物の狼位置を知らないため、誤爆リスクは day 2 以降の黒出しでも常に残る点に注意する。\n"
        "- 黒出しは誤爆リスクを常に見る。人狼位置を知らないため、"
        "真役職・本物の狼・白い位置へ当ててしまうと反動が大きい。\n"
        "- 白出しは破綻しにくいが、白先が本物の狼とは限らない。"
        "白先を確定の味方として扱わず、公開ログ上の反応を見て支援先を調整する。\n"
        "- 霊媒師騙りは真霊媒をローラーに巻き込める一方、自分も処刑されやすい。"
        "占い師 CO 数、霊媒 CO 数、残り縄を見て選ぶ。\n"
        "- 騎士騙りは終盤や対抗騎士が出た場面では有効だが、護衛履歴の破綻リスクが高い。"
        "自分護衛・同じ相手の連続護衛・死亡済み対象への護衛・存在しない護衛成功主張を含めてはならない。\n"
        "- 既に対抗占い師 CO が出ている場合は、day 2 以降に霊媒師騙りまたは騎士騙りを検討する。"
        "霊媒師騙りでは前日処刑者への霊媒結果 (夜に能力を使った想定) を添えて CO する。"
        "騎士騙りでは護衛先 (夜に能力を使った想定) を、"
        "平和な朝ならば護衛成功主張も添えて CO する。\n"
        "- 役職 CO と対抗 CO が合計 6 人以上に膨らむと、"
        "役職 CO していない位置の白が確定する。"
        "騙りすぎには注意する。狂人は本物の狼位置を知らない前提で動くため、"
        "自分が騙り続けるほど推理材料が減る点にも留意する。"
    ),
    Role.SEER: (
        "- 自分の判定履歴を時系列で一貫して扱う。過去の白黒と矛盾する発言はしない。\n"
        "- 黒結果は強い根拠として扱ってよい。ただし対抗 (偽占い) がいる場合は整合性を比較する。\n"
        "- 白結果は『本物の人狼ではない』ことしか保証しない。狂人は白に出るため、"
        "完全な村置きとしては扱わない。\n"
        "- CO タイミング・対抗 CO の有無・投票と判定の噛み合いを重視し、"
        "偽占い視点の破綻を探す。\n"
        "- 公開ログ上まだ占い師 CO が出ていない状態で議論が進む場合は、"
        "初日ランダム白と以後の占い結果を時系列で出して早めに CO する選択肢を強く持つ。"
        "真占いが沈黙し続けると、偽 CO を単独真として扱わせてしまう。\n"
        "- 偽占い師 CO が出た場合は、原則として早めに対抗 CO し、"
        "初日ランダム白を含む全判定履歴を時系列で公開する。潜伏を続けるなら理由が必要。\n"
        "- 黒を引いた場合は、CO して黒結果・過去の白結果・投票理由を明示し、"
        "その黒の処刑と霊媒結果での確認を提案する選択肢を検討する。"
    ),
    Role.MEDIUM: (
        "- 処刑結果と占い師の主張・投票の流れを照合し、占い視点の真贋を見極める。\n"
        "- 自分の霊媒結果が占い視点に与える影響 (真占い補強、偽占い否定など) を整理して発言する。\n"
        "- 処刑された相手が狂人でも、霊媒結果は『人狼ではありませんでした』になる。"
        "黒になるのは本物の人狼だけで、白結果だけでは村置き確定にはならない。\n"
        "- 処刑が発生した翌日は、霊媒結果を公開して議論の軸を作る価値が高い。"
        "沈黙すると偽霊媒 CO を単独真として扱わせてしまうリスクがある。\n"
        "- 処刑がまだ発生していない段階では断定を増やしすぎず、"
        "占い師 CO への反応を観察する。\n"
        "- 対抗霊媒が出た場合は、自分の結果履歴と相手の矛盾を時系列で整理し、"
        "ローラーで自分も巻き込まれる可能性を織り込んで発言する。\n"
        "- 占い師 CO を処刑して霊媒結果が白だった場合、それは占い師 CO 偽の証明ではない。"
        "真占い師だった可能性と、狂人など非狼の騙りだった可能性を分けて整理する。\n"
        "- 占い師 CO を偽視する場合は、霊媒白そのものではなく、"
        "対抗 CO、占い結果の破綻、発言時系列、投票、襲撃結果、死亡タイミングとの整合性を根拠にする。"
    ),
    Role.KNIGHT: (
        "- 守る価値の高い情報役 (真占い・真霊媒) や、信頼されている位置を護衛対象として意識する。\n"
        "- 同じ相手を連続で護衛してはならない。前夜と違う相手を選ぶ。\n"
        "- 自分を護衛対象にすることはできない。死亡済みの相手を護衛したと主張してもならない。\n"
        "- 自分の護衛先を不用意に公開しない。公開すると翌夜の噛み筋のヒントを"
        "人狼側に与えてしまう。\n"
        "- 通常の進行中は潜伏を優先する。根拠のない CO は引き続き避ける。\n"
        "- 犠牲者が出ない平和な朝は、自分の護衛が成功した可能性が高い。"
        "このときは護衛先を添えて騎士 CO する価値が高く、"
        "守った相手を真寄り・白寄りに置く材料として村の推理を進められる。\n"
        "- 終盤、または自分が吊られそうな局面、自分の CO で確白や真役職を守れる局面では、"
        "護衛履歴を日付順に添えて CO することを検討する。\n"
        "- 護衛成功を理由に CO するときは必ず護衛先を添える。"
        "護衛先を隠した騎士 CO は真偽判定されにくく信用されない。\n"
        "- 騎士 CO の護衛履歴は合法でなければならない。"
        "自分護衛・同じ相手の連続護衛・死亡済み対象への護衛・存在しない護衛成功主張は含めない。"
    ),
    Role.VILLAGER: (
        "- 公開発言の矛盾、視点漏れ、投票理由、占い/霊媒結果との整合性を重視して推理する。\n"
        "- 不確実なときは候補を絞り、理由を添えて話す。曖昧な決めつけや"
        "『なんとなく怪しい』だけの発言は避ける。\n"
        "- 自分に私的情報があるふりをしない。占い/霊媒/騎士の CO 騙りは村陣営としては行わない。\n"
        "- 「村人CO」「素村CO」「普通の村人です」「役職は村人です」のように、"
        "自分から村人役職を CO して信用を取ろうとしない。"
        "村人は能力結果を持たないため CO しても証明にはならず、熟練者は村人 CO を信用材料に使わない。\n"
        "- 役職について聞かれた場合も「役職 CO はない」「非 CO の灰」と答えるに留め、"
        "「村人を CO する」形にしない。CO ではなく、公開ログ・CO 履歴・判定履歴・投票履歴・噛み筋・縄数の"
        "整合性で白さを取る。\n"
        "- 情報役を守り、人狼陣営が狙いやすい位置 (真 CO、盤面整理役) を"
        "投票で落とさないようにする。\n"
        "- 発言の根拠は CO 履歴・判定履歴・投票履歴・噛み筋・縄数のうち今の結論に効く 1〜2 点に絞り、"
        "誰のどの発言・票・判定を見たかを短く添える。"
    ),
}


def _build_strategy_block(role: Role) -> str:
    """Return role-specific tips for the given role only.

    Caller must pass a non-None Role; `build_system_prompt` is invoked after
    SETUP so `player.role` is already assigned. Strictly role-scoped — never
    returns other roles' tips, so the system prompt cannot leak strategy
    between LLM seats.
    """
    return _ROLE_STRATEGIES[role]


def _build_speech_profile_block(persona: Persona) -> str:
    """Render the persona's structured speech profile as a bullet block.

    Dispatches on `narration_mode`: silent-gesture personas (kukrushka) get a
    structurally different block — no `一人称` line, gesture examples instead —
    so callers can assert the structural difference in tests. Per-persona
    `forbidden_overuse` carries character-specific overuse bans only; generic
    rules (``1 発話に 1 個まで`` etc.) live in the markdown template.
    """
    sp = persona.speech_profile
    if sp.narration_mode == "silent_gesture":
        forbidden = "、".join(sp.forbidden_overuse) if sp.forbidden_overuse else "(なし)"
        return (
            "- 叙述モード: 原作準拠で『ほぼ無言』。通常の会話文体では発話しない。\n"
            "- `public_message` は短い所作・身振り・表情の叙述文として書く。\n"
            "  例: 『微笑む』『首をかしげる』『手を引く』『うなずく』『見つめる』。\n"
            "- 必要最低限の極短い言語化は許容するが、他キャラのような会話調にはしない。\n"
            f"- 使ってはいけないもの: {forbidden}"
        )
    aliases = "、".join(sp.self_reference_aliases) if sp.self_reference_aliases else "(なし)"
    signatures = (
        "、".join(f"『{p}』" for p in sp.signature_phrases) if sp.signature_phrases else "(なし)"
    )
    forbidden = "、".join(sp.forbidden_overuse) if sp.forbidden_overuse else "(なし)"
    return (
        f"- 一人称: 『{sp.first_person}』\n"
        f"- 自己呼称の例外 (低頻度で使ってよい): {aliases}\n"
        f"- 他者呼称: {sp.address_style}\n"
        f"- 文体とテンポ: {sp.sentence_style}\n"
        f"- 間の取り方: {sp.pause_style}\n"
        f"- 使える短い特徴語 (低頻度、1 発話に多くて 1 個): {signatures}\n"
        f"- 使いすぎ禁止: {forbidden}"
    )


def build_system_prompt(
    persona: Persona,
    role: Role,
    phase: Phase,
    day_number: int,
    task_text: str,
) -> str:
    template = _load_template()
    persona_block = (
        f"名前: {persona.display_name}\n"
        f"性格指針: {persona.style_guide}\n"
        "この人格を口調と判断傾向で表現してください。"
    )
    role_block = (
        f"あなたの役職は『{ROLE_JA[role]}』です。 役職に見える情報だけを根拠にしてください。"
    )
    phase_block = f"`{phase.value}` / day {day_number}"
    return (
        template.replace("{game_rules_block}", _build_game_rules_block())
        .replace("{persona_block}", persona_block)
        .replace("{speech_profile_block}", _build_speech_profile_block(persona))
        .replace("{role_block}", role_block)
        .replace("{strategy_block}", _build_strategy_block(role))
        .replace("{phase_block}", phase_block)
        .replace("{task_block}", task_text)
    )


_VILLAGE_STARTING_ROPES = 4


def _format_rope_block(players: Sequence[Player]) -> str:
    alive = sum(1 for p in players if p.alive)
    dead = len(players) - alive
    ropes_left = max(0, (alive - 1) // 2)
    if alive >= 6:
        risk = f"残り処刑回数の目安: {ropes_left} 縄。終盤までは通常進行。"
    elif alive >= 4:
        risk = f"残り処刑回数の目安: {ropes_left} 縄。PP/RPP の可能性を確認してください。"
    elif alive == 3:
        risk = f"残り処刑回数の目安: {ropes_left} 縄。最終局面: PP/RPP に厳重注意。"
    else:
        risk = f"残り処刑回数の目安: {ropes_left} 縄。決着局面。"
    return (
        "## 縄数・PP/RPPリスク\n"
        f"- 生存 {alive} 人 / 死亡 {dead} 人。{risk} "
        f"(9人村開始時は{_VILLAGE_STARTING_ROPES}縄)\n"
        "- 注意: 残り人狼数と狂人生存は公開情報から推定する必要があります。"
    )


def build_user_context(
    game: Game,
    me: Player,
    my_seat: Seat,
    seats: Sequence[Seat],
    players: Sequence[Player],
    public_logs: Sequence[dict[str, object]],
    private_logs: Sequence[dict[str, object]],
    last_own_public: str | None = None,
) -> str:
    seats_by_no = {s.seat_no: s for s in seats}
    alive_players = [p for p in players if p.alive]
    dead_players = [p for p in players if not p.alive]
    alive_names = "、".join(seats_by_no[p.seat_no].display_name for p in alive_players) or "(なし)"
    dead_names = "、".join(seats_by_no[p.seat_no].display_name for p in dead_players) or "(なし)"

    def _format_log(log: dict[str, object], *, attributed_kinds: tuple[str, ...]) -> str:
        kind = str(log.get("kind", ""))
        text = str(log.get("text", ""))
        actor_seat = log.get("actor_seat")
        if kind in attributed_kinds and isinstance(actor_seat, int) and actor_seat in seats_by_no:
            speaker = seats_by_no[actor_seat]
            return f"- [{kind}] 席{speaker.seat_no} {speaker.display_name}: {text}"
        return f"- [{kind}] {text}"

    priv_lines = [_format_log(log, attributed_kinds=("WOLF_CHAT",)) for log in private_logs[-20:]]
    priv_block = "\n".join(priv_lines) if priv_lines else "(なし)"

    pub_lines = [_format_log(log, attributed_kinds=("PLAYER_SPEECH",)) for log in public_logs[-40:]]
    pub_block = "\n".join(pub_lines) if pub_lines else "(まだ発言なし)"

    last_own = last_own_public or "(まだ発言していません)"

    wolf_partner_block = ""
    if me.role is Role.WEREWOLF:
        partner_tokens = [
            f"席{seats_by_no[p.seat_no].seat_no} {seats_by_no[p.seat_no].display_name}"
            for p in alive_players
            if p.role is Role.WEREWOLF and p.seat_no != me.seat_no and p.seat_no in seats_by_no
        ]
        if partner_tokens:
            wolf_partner_block = (
                "\n## 仲間の人狼 (村人には非公開)\n" + "、".join(partner_tokens) + "\n"
            )

    rope_block = _format_rope_block(players)

    return (
        f"あなたは座席 {my_seat.seat_no}『{my_seat.display_name}』です。\n"
        f"生存者: {alive_names}\n"
        f"死亡者: {dead_names}\n"
        f"現在フェイズ: {game.phase.value} / day {game.day_number}\n"
        f"{wolf_partner_block}"
        "\n"
        f"{rope_block}\n"
        "\n"
        "## あなたの私的メモ (他者には非公開)\n"
        f"{priv_block}\n"
        "\n"
        "## 公開ログ要約 (直近)\n"
        f"{pub_block}\n"
        "\n"
        "## 自分の直近の発言\n"
        f"{last_own}"
    )


# ---------------------------------------------------------- task blocks
def task_daytime_speech(day_number: int) -> str:
    return (
        f"現在は day {day_number} の議論フェイズです。"
        " 必要と感じた場合のみ `intent=speak` を返し、`public_message` に 80〜300 字で短い発言を書いてください。"
        " 発言したくない場合は `intent=skip` と明示してください。"
    )


def task_vote(candidate_tokens: Sequence[str], runoff: bool) -> str:
    """Candidates are `席{N} {display_name}` tokens; target_name must echo one back."""
    names = "、".join(candidate_tokens)
    runoff_note = "これは決選投票です。" if runoff else ""
    return (
        f"{runoff_note}投票先として合法な候補は: {names}\n"
        " `intent=vote`、`target_name` に候補トークン (例: `席3 Alice`) のいずれかを"
        " 厳密に一致させて返してください。`席番号` を含めないと同名の別席と区別できません。"
        " どうしても棄権したい場合は `intent=skip` を返し、`target_name` は `null` にします。"
    )


def task_night_action(kind: SubmissionType, candidate_tokens: Sequence[str]) -> str:
    """Candidates are `席{N} {display_name}` tokens; target_name must echo one back."""
    names = "、".join(candidate_tokens)
    label = {
        SubmissionType.WOLF_ATTACK: "襲撃",
        SubmissionType.SEER_DIVINE: "占い",
        SubmissionType.KNIGHT_GUARD: "護衛",
    }[kind]
    extra = ""
    if kind is SubmissionType.WOLF_ATTACK:
        extra = (
            " 仲間の人狼が人狼チャットで案を出している場合、強い反対理由がなければ"
            " その案に合わせてください。意見が割れると襲撃が空振りになります。\n"
            " 候補ごとに「襲撃価値」「護衛されやすさ」「騎士候補度」「翌日の説明しやすさ」"
            "を短く比較してから 1 名に決めてください。"
            " 単独真寄りの情報役・確白寄り・進行役は襲撃価値も護衛されやすさも高い両刃です。"
            " 騎士っぽい相手を「騎士探し」として先に噛む選択も忘れずに検討してください。"
        )
    return (
        f"夜です。{label} 対象を 1 名選んでください。合法候補: {names}\n"
        " `intent=night_action`、`target_name` に候補トークン (例: `席3 Alice`) のいずれかを"
        " 厳密に一致させて返してください。`席番号` を含めないと同名の別席と区別できません。"
        f"{extra}"
    )


def task_wolf_chat(partner_tokens: Sequence[str], candidate_tokens: Sequence[str]) -> str:
    """Ask a wolf to post a short coordination message to the wolves-only chat."""
    partners = "、".join(partner_tokens) if partner_tokens else "(なし)"
    names = "、".join(candidate_tokens)
    return (
        f"夜になりました。仲間の人狼: {partners}。人狼チャット (村人には非公開) で"
        f" 襲撃対象を調整してください。候補: {names}\n"
        " `intent=speak` と `public_message` に 1 名の襲撃候補とその理由を"
        " 80〜150 字で書いてください。"
        " 理由には「襲撃価値 (情報役噛み/白位置噛み/意見噛み/騎士探し/SG 残しのどれか)」"
        "「護衛されそうか」「本人が騎士っぽいか (騎士候補)」「相方案への賛否」のうち"
        " 重要な 1〜2 点を含めてください。"
        " 仲間が既に案を出している場合は、護衛リスクと襲撃価値を比較したうえで"
        " 最終的に 1 人に揃えることを優先してください。"
        " 話すことがなければ `intent=skip` を返してください。"
    )


__all__ = [
    "FACTION_JA",
    "build_system_prompt",
    "build_user_context",
    "task_daytime_speech",
    "task_night_action",
    "task_vote",
    "task_wolf_chat",
]
