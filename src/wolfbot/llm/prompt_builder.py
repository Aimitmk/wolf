"""Construct system + user messages for xAI calls.

Public functions build plain-string prompts so the xAI layer can stay transport-agnostic.
Inputs are domain models; outputs are strings. No I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from pathlib import Path

from wolfbot.domain.enums import (
    FACTION_JA,
    ROLE_DISTRIBUTION,
    ROLE_JA,
    VILLAGE_SIZE,
    Phase,
    Role,
    SubmissionType,
    format_co_claim_options,
)
from wolfbot.domain.models import Game, Player, Seat
from wolfbot.llm.persona_base import Persona
from wolfbot.llm.template import load_template, render_template

SYSTEM_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "prompts" / "llm_system_prompt.md"


def _load_template() -> str:
    return SYSTEM_TEMPLATE_PATH.read_text(encoding="utf-8")


_GAME_RULES_TEMPLATE = "shared/game_rules_9p"


def _build_game_rules_block() -> str:
    """Return the fixed 9-player ruleset shared by every LLM seat.

    Body lives in `prompts/templates/shared/game_rules_9p.md` so the
    canonical ~110-bullet ruleset is editable in plain markdown without
    a Python diff. Two placeholders are filled here: ``village_size``
    (= ``VILLAGE_SIZE`` constant) and ``distribution`` (rendered from
    ``ROLE_DISTRIBUTION`` + ``ROLE_JA`` so the canonical seat counts
    stay derived, not duplicated). Win conditions and the invariants
    the LLM must never violate (NIGHT_0 random white is non-wolf,
    seer/medium see only real wolves as black, wolves split → attack
    fails, knight can't guard the same target twice, `target_name`
    must match a candidate token, etc.) are encoded as fixed text
    inside the template.

    The legacy concatenated-string form below is kept as the canonical
    Japanese reference for the template; updating one without the
    other will fail the round-trip test in
    ``test_template_engine_integration``.
    """
    distribution = " / ".join(
        f"{ROLE_JA[role]}{count}" for role, count in ROLE_DISTRIBUTION.items()
    )
    return render_template(
        _GAME_RULES_TEMPLATE,
        village_size=VILLAGE_SIZE,
        distribution=distribution,
    )


def _build_game_rules_block_legacy_inline() -> str:
    """Original inline form retained for the round-trip parity test.

    Do NOT call from production code paths — the live code uses
    :func:`_build_game_rules_block` which loads the markdown template.
    This function exists so a unit test can compare the inline string
    against the rendered template and fail loudly the moment they
    drift apart, catching accidental edits to only one side.
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
        "- 占い/霊媒の判定色は黒 (本物の人狼)/白 (本物の人狼ではない) の 2 値のみ。"
        "灰・グレー・不明・保留・確定不能など第 3 の色はこの bot のルール上存在せず、"
        "いかなる役職 (真/騙りを含む) も第 3 の色を判定として主張してはならない。"
        "CO 者が第 3 の色を判定として出した時点で、その CO は破綻として確定扱いし、"
        "聞き手側は他のすべての判定材料に優先してその CO を切る根拠にしてよい。\n"
        "- 霊媒結果の白 (『人狼ではありませんでした』) は、対象が本物の人狼ではないことだけを示す。"
        "役職名 (占い師・霊媒師・騎士・村人・狂人) までは特定できない。\n"
        "- 処刑された占い師 CO に霊媒結果で白が出ても、真占い師だった可能性と矛盾しない。"
        "霊媒白だけを理由にその占い師 CO を偽扱いしない。"
        "偽視するなら、対抗 CO、占い結果の破綻、発言時系列、投票、襲撃結果、死亡タイミングとの整合性で判断する。\n"
        "- 逆に処刑された占い師 CO に霊媒結果で黒が出た場合は、その人物は本物の人狼なので、"
        "真占い師ではなく人狼の騙りだったと強く判断してよい。\n"
        "- NIGHT_0 に占い師へ提示されるランダム白は、本物の人狼ではない相手が選ばれる。"
        "ただし真に村であることは保証されない (狂人の可能性はある)。\n"
        "- **NIGHT_0 (= 初日夜・ゲーム開始直後の最初の夜) には人狼の襲撃も騎士の護衛も発生しない。**"
        "発生する夜行動は占い師の初回ランダム白だけで、それ以外の役職は何も行動しない。"
        "そのため day 1 (1日目) の朝は構造的に必ず『平和な朝』になり、"
        "『平和な朝』が **守られたことの根拠にも襲撃失敗の根拠にも一切ならない**。"
        "day 1 朝の平和は『誰かが護衛した』『騎士のGJ』『襲撃が失敗した』のいずれの解釈とも結びつかない。"
        "そのような解釈・推理を発話に出すと **構造ルール違反として破綻**として扱われる。\n"
        "- **day 1 朝の段階で占い師 (真/騙りを問わず) が公的に提示できる占い結果は NIGHT_0 のランダム白 1 件のみ。**"
        "NIGHT_1 はまだ発生していないため、day 1 朝に語る『昨夜』は NIGHT_0 を指し、結果は必ず白で対象も 1 人だけ。"
        "day 1 朝に 2 件目以降の占い結果を主張する (例: 同じ朝に『コメットを占って白だった』と言ってから別ターンで"
        "『ジナを占って白だった』と続ける) のは時系列上不可能で、即座に偽占いとして切る根拠になる。"
        "同様に **day 1 朝の霊媒結果は構造的に存在しない** (前日の処刑がまだ無いため)。"
        "day 1 朝に霊媒結果を語った時点で偽霊媒確定として扱う。\n"
        "- **自分が公の場で主張した占い結果・霊媒結果・護衛履歴は、後のターンで対象や色を変えてはならない。**"
        "同じ夜について『Alice を占った白』と言った後で別ターンで『Bob を占った』と言い直したり、"
        "過去に白と言った対象を後から黒に塗り替えたりするのは、本物の役職者には絶対に起き得ない構造的矛盾である。"
        "聞き手側はそのような対象差し替え・色反転を観測した時点で、その CO を即座に偽として切る根拠にしてよい。"
        "日が進んで新しい夜行動の結果を公表する (例: day 2 の朝に day 1 夜の占い結果を新規発表) のは可能だが、"
        "それは『過去に発表済みの結果に新しい行を追加する』操作であり、過去の行を書き換える操作ではない。\n"
        "- 人狼は day 1 の夜 (= NIGHT_1) から襲撃を行い、騎士も day 1 の夜から護衛できるようになる。"
        "GJ (グッジョブ) や護衛成功・襲撃失敗の議論が初めて意味を持つのは day 2 の朝以降である。"
        "day 1 朝の時点ではまだ夜行動の結果は何も発生していないが、"
        "**人狼 2 名は最初から村に存在している** (NIGHT_0 で襲撃しないだけで、潜伏中)。"
        "『初日朝に死んでいないから人狼はまだ動いていない / 人狼疑いの根拠が無い』というのも誤りで、"
        "人狼は day 1 の議論・投票で吊り逃れを狙って発言・誘導している前提で疑い始めてよい。\n"
        "- 人狼同士で夜の襲撃対象の意見が割れた場合、Master 側で最終的に必ず 1 つの対象が確定する: "
        "(a) 片方が人間プレイヤーで片方が LLM 席のときは人間プレイヤーの選択がそのまま採用される、"
        "(b) 双方 LLM の不一致では Master が 2 つの選択肢からランダムに 1 つを採用して襲撃を成立させる、"
        "(c) 双方人間で不一致のときも Master がランダムに 1 つを採用する。"
        "つまり人狼同士で襲撃先が割れても空振りにはならず、どちらか片方の選択が必ず実行される。"
        "意図的に割って撹乱を狙っても、もう片方の人狼の襲撃先が選ばれる確率も同じだけあるため得にならない。"
        "狙いを揃えた方が情報役を確実に噛める利点が大きいので、"
        "人狼専用チャットで襲撃先を 1 人に揃えることを最優先にする。\n"
        "- **夜の襲撃で死亡した席は本物の人狼ではない (人狼は仲間を襲わないため、"
        "公開ログで `(襲撃)` 表示の死亡席は構造的に非狼確定)。**"
        "誰かが公の場で『襲撃された席は人狼だった』『襲撃で沈んだ者こそ狼だった』のように主張した場合、"
        "それは構造ルールに反する明白な嘘であり、その発言者を強い人狼候補 (騙りや狂人を含む狼陣営位置) として扱ってよい。"
        "また聞き手側がこの嘘に同調・追認する発話 (『なるほど襲撃された◯◯は狼だったんだね』等) も"
        "村陣営にとっては破綻発言で、その発言者の信用も落ちる。"
        "霊媒結果より優先される HARD ファクトとして扱う。\n"
        "- 騎士は同じ相手を連続で護衛できない (前夜と同じ対象は選べない)。\n"
        "- **同一役職の CO 数には構造的な上限がある。**"
        "占い師は 1 (真) + 2 (人狼が騙れる) + 1 (狂人が騙れる) で **理論最大 4** だが、"
        "**実戦では公開ログ上の占い CO が通算 3 件に達した時点で、4 件目を出すのは戦略的に破綻**する "
        "(村陣営の村人 1 名が騙りに出る合理的理由がなく、4 件目 CO は強い狼/狂人疑いを浴びるため)。"
        "霊媒師は 1 (真) + 1 (人狼か狂人の騙り) で **理論最大 2**、3 件目以降は出した時点で偽確定。"
        "騎士は 1 (真) + 1 (人狼/狂人の騙り) で **理論最大 2**、3 件目以降は偽確定。"
        "聞き手側はこの上限を超えた CO を観測した時点で、その CO 者を強い狼陣営疑いとして扱う。\n"
        "- 投票先や夜行動対象は、プロンプトで提示された合法な候補トークン "
        "(例: `席3 Alice`) の中からだけ選ぶ。候補外の名前を返してはならない。\n"
        "- 特定役職 (占い師・霊媒師・騎士) の CO が 1 人だけで、同じ役職への対抗 CO が"
        "公開ログ上一度も出ていない場合、その単独 CO 者は原則として真の役職者にかなり近い位置として扱う。"
        "根拠なくその CO 者を処刑候補にしない。\n"
        "- **特に初日朝 (まだ誰も死亡していない時点) の単独 CO は、真として扱う。**"
        "初日朝はまだ霊媒結果も襲撃結果もなく、対抗 CO が出る時間も十分に残っているため、"
        "単独で CO した者を疑う公開情報は実質まだ存在しない。"
        "それでも疑う発話・投票を行うと『初日に CO したから怪しい』という根拠にならない疑い方になり、"
        "村陣営にとって最大の損失になる。"
        "対抗 CO がその後も出ず、判定矛盾・票筋・襲撃結果といった具体的破綻もない限り、"
        "初日の単独 CO 者は真置きで進行する。\n"
        "- ただし単独 CO は絶対確定ではない。公開ログ上の発言破綻・投票矛盾・判定結果の矛盾・"
        "噛み筋との不整合など、通常より強い根拠がある場合に限り疑ってよい。\n"
        "- **単独 CO 者を疑う『強い根拠』には、具体的な公開情報の矛盾が必要である。**"
        "他のプレイヤー (人間プレイヤー含む) が『〜は怪しい』『〜は人狼』と単に表明しただけ、"
        "占い結果や霊媒結果の具体的な指摘・投票や噛み筋の矛盾の指摘がない直感的疑い表明は、"
        "単独 CO 真置きルールを上書きする根拠としては弱い。"
        "そうした表明に同調して単独 CO 者へ票を入れる前に、自分視点で『単独 CO 者を切る具体的な公開情報』が"
        "1 つでも挙げられるかを確認する。挙げられないなら同調しない。"
        "特に真役職を持つ自分は、公開情報と矛盾しない単独 CO を切ると村の情報を失う側に動くことになる。\n"
        "- 人間プレイヤーの直感的な疑い表明は、議論を再評価するきっかけとして参照してよいが、"
        "それ自体を独立した証拠として扱わない。人間も狼/狂人として騙る可能性があるし、"
        "村役でも誤推理するため、根拠の中身 (具体的な公開情報の指摘) で重み付けする。\n"
        "- ただし「現在生存している CO 者が 1 人だけ」というだけでは単独 CO 扱いしない。"
        "同じ役職 CO が過去に 2 人以上存在したことがある場合、対抗者が処刑・襲撃などで死亡して"
        "現在 1 人だけ残っていても、その残存 CO 者を自動的に真置きしない。\n"
        "- **CO 通算件数は『生存・死亡を問わず過去に CO した全 seat の数』で数える。**"
        "プロンプトの `## 公開された占い/霊媒CO結果` ブロックには死亡席の CO も列挙されており、"
        "そのブロックの『通算 N 件』が公式の件数。"
        "「現在生存している占い CO は 1 人」と「占い CO は単独 (= 通算 1 件)」は別の概念で、"
        "後者は通算ベースで判断する。"
        "「占い師は◯◯さんだけ」「単独のCO」「単独で出ているから真」と判断する **前に必ず** "
        "ledger ブロックの通算件数を確認すること。"
        "通算 2 件以上なら、たとえ生存者が 1 人でも『単独 CO』とは呼ばない。"
        "死亡した CO 者の主張は記録に残り、推理材料として依然として有効である。\n"
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
        "- 死亡した席は、過去の発言・投票・判定の信用評価対象としては引き続き議論してよいが、"
        "今日の処刑対象 (vote target) としては議題に含めない。"
        "今日の vote 候補は生存席だけ。死者の信用議論を尽くすこと自体は構わないが、"
        "発言の結論を『死者を吊ろう』『今日◯◯ (死亡席) を処刑すべき』のように"
        "死者を今日の処刑対象として語るのは破綻発言として扱う。\n"
        "- 異端票 (例: 多数派と違う対象に day 1 で投票している席) を狼疑いとして数える前に、"
        "その投票者が真占い視点で動いていた場合に整合するかを必ず一度確認する。"
        "真占いは NIGHT_0 ランダム白で得た情報を持ち、自分視点の黒読み・灰読みで"
        "多数派と違う票を入れる動機が自然に発生する。"
        "後日その異端票の対象が黒判定されて処刑され、霊媒結果でも黒が出た場合、"
        "その異端票は『真占い視点での黒読み先制』として説明される側に強く寄るため、"
        "異端票そのものを狼の異常行動として扱うのは誤読になる。"
        "票の異端さだけで疑うのではなく、判定履歴・霊媒結果・噛み筋との整合性で再評価する。\n"
        "- 自分が役職持ちでまだ未公開の能力結果 (霊媒結果・占い結果・護衛日記など) を抱えている場合、"
        "発言の番が回ってきたとき、その発話の冒頭で必ず CO + 結果公表を行う。"
        "他者から呼びかけられて (addressed) いてその質問に答えたい場合でも、"
        "未公開の能力結果が手元にあるならその公表を最優先にし、"
        "addressed への返答は CO + 結果の後に短く添える。"
        "addressed 文脈に飲まれて CO + 結果を欠落させると、"
        "聞き手側 NPC は構造化フィールド (`co_declaration`) しか見られず"
        "公開ログには CO の自然言語が一切残らないため、"
        "他席は『自分が CO した』ことを認識できない。\n"
        "- 占い師・霊媒師・騎士の各 CO 数を時系列で整理し、各役職について『CO 数 - 1』(下限 0) を「対抗 CO 超過分」(騙り最低数) として数える。"
        "真役職は各 1 人だけのため、超過分はその役職で少なくとも騙りである。\n"
        "- 占い師 CO 超過分 + 霊媒師 CO 超過分 + 騎士 CO 超過分 の超過分合計が 3 に達した場合、"
        "人狼 2 + 狂人 1 の狼陣営 3 名が能力役職 CO 群に出切っている。"
        "村陣営の騙り・CO 撤回・同一人物の複数 CO・曖昧な CO 文言の誤読・死亡済み CO 見落とし・前提破綻がない限り、"
        "能力役職 CO していない位置は配役上の消去法で村陣営の確白級として扱う。"
        "同条件下で対抗のない単独 CO 役職が別にある場合、その単独 CO 者も狼陣営ではないため真役職としてかなり強く扱える。\n"
        "- この超過分合計 3 による非 CO 確白は、単発の白判定 (狂人も白に出るため『村陣営確定』とは言い切らない) とは別根拠で、"
        "固定配役上の消去法で狼陣営 3 名が CO 群に出切ったと数えられる点で村陣営まで強く推せる。"
        "ただし、対抗 CO 群の中で誰が真役職かまでは超過分合計だけでは特定できない。"
        "判定結果・霊媒結果・投票・襲撃・死亡タイミング・破綻で詰める。\n"
        "- 超過分合計が 0〜2 の段階では、狼陣営が非 CO や単独 CO に残っている可能性があるため、"
        "非 CO 位置を CO 数だけで確白とは断定しない。\n"
        "- 超過分合計が 4 以上に見える場合は固定配役と矛盾する。"
        "CO 撤回、同一人物の複数 CO、話題としての CO 語彙の誤読、村騙り、死亡済み CO 見落とし、ログ見落としを疑い、"
        "確白扱いを保留して時系列を再整理する。\n"
        "- 例: 3-2-1 (占い師 CO 3 / 霊媒師 CO 2 / 騎士 CO 1) → 超過分 2 + 1 + 0 = 3。"
        "前提崩壊がなければ非 CO 位置は村陣営の確白級、対抗のない単独騎士 CO も狼陣営ではないため真騎士寄り。\n"
        "- 例: 2-2-2 (占い師 CO 2 / 霊媒師 CO 2 / 騎士 CO 2) → 超過分 1 + 1 + 1 = 3。"
        "各 CO 群に 1 人ずつ真役職、残り 3 人が狼陣営という形が基本。"
        "非 CO 位置は確白級。各 CO 群内の真偽は判定結果・霊媒結果・投票・襲撃で詰める。\n"
        "- 例: 3-1-1 (占い師 CO 3 / 霊媒師 CO 1 / 騎士 CO 1) → 超過分 2 + 0 + 0 = 2。"
        "狼陣営 1 名が非 CO や単独 CO に残る可能性があるため、非 CO 全員を確白とは断定しない。\n"
        "- 例: 4-1-1 (占い師 CO 4 / 霊媒師 CO 1 / 騎士 CO 1) → 超過分 3 + 0 + 0 = 3。"
        "占い師 CO 群に狼陣営 3 名が固まっている可能性が高く、対抗のない単独霊媒・単独騎士は強い白寄り。"
        "CO 撤回や村騙りが出たら再整理する。\n"
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
        "- 3-1 で占い師 CO が 3 人並んだ盤面では、CO 者のうち 2 人が公開情報上『本物の人狼ではない』と確定した場合、"
        "残る 1 人の占い師 CO を固定配役上の消去法として確定黒級の人狼位置と推定する。"
        "人狼 2 人固定の配役で『占い師 CO 3 人のうち 2 人が非狼確定』なら、"
        "残る占い師 CO 位置に少なくとも 1 人の人狼がいる以外に整合する配役がないためである。\n"
        "- ただし『白判定』と『非狼確定』を混同しない。"
        "信用が未確定な占い師 CO が出した白、偽が混じり得る霊媒結果、印象だけの白寄り評価は非狼確定として数えない。"
        "狂人も白判定されるため、単発の白だけで非狼扱いを固定しない。\n"
        "- 非狼確定として数えてよい根拠は、公開ログ・霊媒結果・襲撃死・真寄り情報役の判定・"
        "CO 破綻整理など、この bot のルールと公開情報の整合から説明できるものに限る。"
        "霊媒師 CO 側が真寄りと十分判断できる時点での霊媒白、本物の人狼ではないと示す襲撃死、"
        "対抗 CO や判定矛盾で偽が破綻した結果としての非狼整理などが具体的な根拠になる。\n"
        "- 2 人非狼確定が成立した場合は、残る占い師 CO を『まだ灰の 1 人』ではなく、"
        "固定配役上の狼位置として投票・発言・進行提案へ反映させる。"
        "黒ストップによる灰精査ではなく、残る占い師 CO の処刑提案や、"
        "その人物を相方候補ペア仮説の片側として扱う議論を優先してよい。\n"
        "- ただし前提が崩れた場合は確定黒扱いを解除して時系列から再整理する。"
        "村陣営の騙り (例: 狂人や村人が占い師 CO していた)、CO 撤回、"
        "霊媒師 CO 側が偽だった可能性が後から浮上した状況、非狼確定の根拠が後から破綻した場合などが該当する。"
        "前提が崩れたと分かった時点で、確定黒扱いをやめ、CO 履歴と判定履歴を時系列で再整理する。\n"
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
        "- 占い結果・霊媒結果を発言で出すときは、**対象席名 + 判定色 (黒/白) を必ず一対一で添える**。"
        "例: 『セツさんは白でした』『コメットを占って黒、ジナを占って白』のように、"
        "各対象を名指しで列挙する。"
        "**対象を特定せずに『すべて白』『全員白』『全部白』『みんな白』のように"
        "複数件をまとめて主張するのは破綻発言**として扱う。"
        "聞き手はそうした主張をした CO を破綻確定として切ってよい。\n"
        "- 特に day 1 朝の占い師 CO は、NIGHT_0 のランダム白 1 件しか手元にない。"
        "**day 1 の占い結果は必ず対象 1 名 + 白判定の 1 件のみ**で、"
        "複数対象の主張や『すべて白』のような対象不明な白主張は実ルール上ありえないため、"
        "その時点で CO 破綻として扱う。\n"
        "- 偽占い師の黒結果主張は day 2 以降にのみ行う。"
        "前夜に占ったという想定で、公開ログ・生存状況・過去に自分が出した判定履歴・"
        "対抗 CO の発表内容と矛盾しない場合だけ出す。\n"
        "- 偽霊媒師は、処刑がなかった日に霊媒結果を捏造しない。\n"
        "- 偽騎士は、自分護衛・同一対象連続護衛・死亡済み対象への護衛・存在しない護衛成功を主張しない。\n"
        "- どの偽 CO でも、実際には知らない他者役職や狼位置を事実として断言しない。\n"
        "- 自分が占い師/霊媒師として一度公表した判定 (対象 + 黒/白) は、"
        "原則として後日撤回・色変更・対象差し替えをしない。"
        "前夜の能力使用結果として一度言い切った内容は、その後の発言でも同じ対象を同じ色で扱う。"
        "打ち間違いに気づいた場合のみ、『訂正』『失礼、◯◯でした』などの訂正文言を明示してから直し、"
        "訂正後の内容を以降の発言でも保持する。\n"
        "- プロンプトの『## 公開された占い/霊媒CO結果 (公式記録)』ブロックは、"
        "Master が `claimed_seer_result` / `claimed_medium_result` の構造化フィールドから"
        "ビルドした各 CO 者の累積発表履歴である。各 CO 者の過去の発表結果はここに完全に残る。\n"
        "- 自分が占いCO/霊媒CO 者である場合、新しい結果を発表する発話では"
        "この公式記録に列挙された自分の過去結果と完全に整合する内容のみを発表する。"
        "過去に発表した対象・色を別の組み合わせに差し替える、過去結果の片方を黙って削って違う対象を追加する、"
        "対抗 CO 者の発表内容を自分の結果として取り込む — これらは全て破綻として扱われる。\n"
        "- 占い CO 者は day N の朝までに通算 N + 1 件の結果を持つ "
        "(NIGHT_0 のランダム白 + 各夜 1 件)。"
        "通算件数が記録より少ない/多い結果列を主張すると **数の破綻** として確定し、"
        "聞き手はその CO を直ちに切ってよい根拠とする。"
        "霊媒 CO 者は処刑があった日数だけ結果を持つ "
        "(処刑なしの日は『結果なし』を明言する)。\n"
        "- 構造化フィールドと発話内容は必ず一対一で一致させる。"
        "新しい占い結果を述べる発話では `claimed_seer_result.target_seat`・`is_wolf` を"
        "発話内容と同じ対象・色で必ず設定する。霊媒も同様に `claimed_medium_result` を使う。"
        "新しい結果を発表しない発話 (前回までの結果に言及するだけ・他人への質問・一般議論) では"
        "両フィールドとも null にする。発話内容と構造化フィールドが食い違うと、"
        "Master 側の整合検査で破綻として記録される。\n"
        "- 同じ対象への判定色が前日と当日で食い違った CO を見つけた場合、"
        "人間プレイヤーの言い間違い・打ち間違いの可能性が残るため即座に偽 CO 確定とはしない。"
        "ただし強い偽要素として推理に組み入れ、明示的な訂正文言なしに食い違いが続く場合は、"
        "他の偽要素 (対抗 CO・票筋・噛み筋・破綻) と合わせて切ってよい材料として扱う。\n"
        "- day 2 以降に占い師・霊媒師・騎士として CO 中の者は、真でも偽でも、"
        "昼の議論 1 巡目で前夜相当の能力結果を出すのが信用上重要である。"
        "結果を持つはずの役職 CO が 1 巡目で結果を出さないと、信用低下や破綻疑いにつながる。\n"
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
        "- 騎士 / 狩人 / 狩: この bot の正式役職名は騎士。"
        "人狼用語として狩人・狩も同じ護衛役を指す同義語として使われるため、"
        "公開ログ上で狩人や狩と書かれていても騎士と同じ意味として読む。\n"
        "- 鉄板護衛: 真寄り情報役・確白寄り・進行役など、"
        "噛まれると村が大きく崩れる位置を堅く守る護衛。\n"
        "- 変態護衛: セオリー上の本命から外れた位置を、襲撃読みで守る護衛。"
        "当たれば強いが、外すと重要役職を抜かれるリスクがある。\n"
        "- 捨て護衛: 噛まれにくい、または噛まれても村損失が小さい位置を"
        "あえて護衛する戦術。連続護衛不可の環境では、今日本命を守ると"
        "明日その本命を守れなくなるため、次夜の本命護衛余地を残す目的でも使う。"
        "この bot では合法護衛候補から 1 名を選ぶ行動であり、"
        "未提出・対象なし・誰も守らない・skip ではない。\n"
        "- 連ガ無し / 連続護衛不可: 同じ相手を連続で護衛できないルール。"
        "この bot は連続護衛不可で、前夜の護衛先は今夜の合法候補から外れる "
        "(前述の騎士護衛ルールと同じ)。\n"
        "- 護衛読み: 人狼がどこを噛みたいか、"
        "どこが護衛されていそうで噛みを避けるかを推理すること。"
        "騎士側でも、自分の護衛が読まれて噛みを外される可能性を考える材料になる。\n"
        "- 護衛誘導: 騎士の護衛先に影響を与えようとする昼発言。"
        "村利の進行整理にもなれば、人狼側の誘導にもなり得るため、"
        "発言者の立場・噛み筋・投票・翌日の得を合わせて評価する。\n"
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
        "長い内部思考そのものを発話しない。\n"
        "- 比較・関係を表す語 (重複・ライン・出来レース・囲い・身内切り・対立・連携) を使うときは、"
        "比較対象を必ず明示する。誰の何の発言・判定・投票・夜行動が、誰の何と『重複/ライン』しているのかを "
        "1 件以上具体的に引用する。引用なく関係語だけを並べた主張は推理上の根拠として扱わず、"
        "聞き手側は内容を再確認する。\n"
        "- この村は人狼 2 人固定なので、怪しい人を 1 人挙げたら、その人物が人狼ならもう 1 人の相方候補は誰かまで"
        "公開ログからの仮説として考える。"
        "村側・狂人・確定していない役職は実際の 2 人狼ペアを知らないため、断定ではなく推理として扱う。\n"
        "- A-B の 2 人狼仮説は、(1) 庇い・便乗・距離の取り方、(2) 投票先・決選投票・票変えなどの票筋、"
        "(3) 占い・霊媒結果と白先・黒先・囲い候補、(4) 噛み筋・噛まれなかった位置・情報役噛みとの整合、"
        "(5) 片方が処刑濃厚なときの動き (ライン切り・身内票) が"
        "2 人狼セットとして自然かで検証する。\n"
        "- 単体黒要素が強くても自然な相方候補が見つからない場合は疑いの強さを下げ、"
        "単体では中庸でも相方候補との票筋・噛み筋が強くつながる場合は疑いを上げる。\n"
        "- 発言では 2 人狼候補を長く列挙せず、最も効くペア仮説 1〜2 点だけを短く添える。"
        "全候補のペアを並べる議論は時間を浪費し、結論にもつながりにくい。\n"
        "- 「相方候補」は公開ログからの推理用語として使ってよい。"
        "ただし実際の 2 人狼ペアを知っているのは人狼本人だけで、"
        "それ以外の役職は相方候補を確定情報として語らない。"
    )


# Role-specific tips. Each role has its own markdown file under
# `prompts/templates/strategy/` (e.g. `strategy/werewolf.md`). The
# cross-leak tests assert each file's vocabulary stays role-scoped:
# the wolf playbook's `相方` / `襲撃先を揃える` must not appear in any
# other role's file, and `本物の人狼位置を知っている前提` (a
# prohibition unique to the madman) must not leak elsewhere. When
# editing a strategy file, keep its bullets focused on that one role.
_STRATEGY_FILE_BY_ROLE: dict[Role, str] = {
    Role.WEREWOLF: "strategy/werewolf",
    Role.MADMAN: "strategy/madman",
    Role.SEER: "strategy/seer",
    Role.MEDIUM: "strategy/medium",
    Role.KNIGHT: "strategy/knight",
    Role.VILLAGER: "strategy/villager",
}


@cache
def _load_role_strategy(role: Role) -> str:
    """Read a role's strategy markdown and return the bullet body.

    The on-disk file starts with a `# 人狼 (WEREWOLF) 戦略` heading
    intended for human readers; this helper strips the heading + the
    blank line that follows so the LLM sees only the bullet list. The
    trailing newline appended on file write is also stripped so the
    returned string is byte-equivalent to the legacy inline-dict form
    (relevant for the cross-leak substring tests). Cached so repeated
    `build_system_prompt` calls within one process touch disk once
    per role.
    """
    raw = load_template(_STRATEGY_FILE_BY_ROLE[role])
    parts = raw.split("\n", 2)
    has_md_heading = (
        len(parts) >= 3 and parts[0].startswith("# ") and parts[1] == ""
    )
    body = parts[2] if has_md_heading else raw
    return body.rstrip("\n")


def build_strategy_block(role: Role) -> str:
    """Return role-specific tips for the given role only.

    Caller must pass a non-None Role; `build_system_prompt` is invoked after
    SETUP so `player.role` is already assigned. Strictly role-scoped — never
    returns other roles' tips, so the system prompt cannot leak strategy
    between LLM seats.
    """
    return _load_role_strategy(role)


# Underscore alias retained for the historical "private" import path used
# inside this module; new external callers (Master arbiter → SpeakRequest)
# use the public `build_strategy_block` name.
_build_strategy_block = build_strategy_block


def _band(value: float, *, low: str, mid_low: str, mid: str, mid_high: str, high: str) -> str:
    """Map a 0.0-1.0 axis to one of five qualitative bands.

    Five-step granularity gives the LLM enough nuance without exposing the
    raw float (which would invite spurious precision). Boundaries are
    chosen so the neutral 0.5 default sits squarely on `mid`.
    """
    if value <= 0.2:
        return low
    if value <= 0.4:
        return mid_low
    if value <= 0.6:
        return mid
    if value <= 0.8:
        return mid_high
    return high


def build_judgment_profile_block(persona: Persona) -> str:
    """Render `JudgmentProfile` axes as labeled tendency bands.

    Each axis is mapped to a qualitative band so the LLM has a concrete
    behavioural lean without seeing the raw float. The block is paired
    with a usage hint that names HARD/MEDIUM facts so the trust axes
    have something concrete to attach to.
    """
    j = persona.judgment_profile
    trust_hard = _band(
        j.trust_hard_facts,
        low="ほぼ無視 (理屈より直感)",
        mid_low="やや軽視",
        mid="標準",
        mid_high="重視",
        high="絶対視 (論理確定は揺るがない)",
    )
    trust_medium = _band(
        j.trust_medium_facts,
        low="ほぼ参考にしない",
        mid_low="懐疑的に扱う",
        mid="参考程度",
        mid_high="やや信用する",
        high="基本受け入れる",
    )
    contrarian = _band(
        j.contrarian_bias,
        low="多数派にあえて逆らわない",
        mid_low="やや迎合的",
        mid="是々非々",
        mid_high="多数派に懐疑的",
        high="あえて逆張りする傾向",
    )
    aggression = _band(
        j.aggression,
        low="慎重で疑い先を出すのが遅い",
        mid_low="控えめに疑う",
        mid="標準的に疑い先を出す",
        mid_high="積極的に疑い先を指す",
        high="即座に処刑候補を名指しする",
    )
    bandwagon = _band(
        j.bandwagon_tendency,
        low="単独行動を好み流れに乗らない",
        mid_low="独自路線を好む",
        mid="状況次第",
        mid_high="形成された流れに乗りやすい",
        high="多数派・流れに強く乗る",
    )
    return (
        f"- 論理確定 (HARD ファクト) への態度: {trust_hard}\n"
        f"- 推測根拠 (MEDIUM ファクト) への態度: {trust_medium}\n"
        f"- 多数派への姿勢: {contrarian}\n"
        f"- 攻撃性 (疑い→処刑候補名指しまでの速さ): {aggression}\n"
        f"- 流れへの追従度: {bandwagon}\n"
        "- 上記は判断のクセであり、ルールや論理確定情報を上書きしない。"
        "HARD ファクトは原則として受け入れた上で、態度に応じた言い回しに調整する。"
        "MEDIUM ファクトは「態度」に応じて採用度合いを変える。"
        "この性格を口調と判断の傾きとして表現してください。"
    )


def build_speech_profile_block(persona: Persona) -> str:
    """Render the persona's structured speech profile as a bullet block.

    Public function (renamed from the historical underscored name) so the
    Master arbiter can render the same block for the reactive_voice NPC
    prompt as rounds-mode uses.

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


# Underscore aliases for callers (and tests) that imported the historical
# private names. The reactive_voice NPC system-prompt builder calls the
# public names directly.
_build_speech_profile_block = build_speech_profile_block
_build_judgment_profile_block = build_judgment_profile_block


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
        .replace("{judgment_profile_block}", build_judgment_profile_block(persona))
        .replace("{speech_profile_block}", build_speech_profile_block(persona))
        .replace("{role_block}", role_block)
        .replace("{strategy_block}", build_strategy_block(role))
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
    deduced_facts_block: str | None = None,
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

    facts_section = ""
    if deduced_facts_block:
        facts_section = (
            "\n## 公開情報からの確定/推測事実 (Master 整理)\n"
            f"{deduced_facts_block}\n"
            "HARD は論理的に確定。MEDIUM は強めの推測。"
            "判断傾向に応じて態度を変えてよいが、HARD を覆す論拠は公開ログにある具体物だけにする。\n"
        )

    return (
        f"あなたは座席 {my_seat.seat_no}『{my_seat.display_name}』です。\n"
        f"生存者: {alive_names}\n"
        f"死亡者: {dead_names}\n"
        f"現在フェイズ: {game.phase.value} / day {game.day_number}\n"
        f"{wolf_partner_block}"
        "\n"
        f"{rope_block}\n"
        f"{facts_section}"
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
_TASK_DAYTIME_SPEECH_TEMPLATE = "master/task_daytime_speech"
_TASK_VOTE_TEMPLATE = "master/task_vote"
_TASK_NIGHT_ACTION_TEMPLATE = "master/task_night_action"
_TASK_WOLF_CHAT_TEMPLATE = "master/task_wolf_chat"


def task_daytime_speech(
    day_number: int,
    discussion_round: int | None = None,
    *,
    role: Role | None = None,
) -> str:
    """Day-discussion task instruction.

    Body lives in ``master/task_daytime_speech.md``. Two optional
    paragraphs are gated by template ``{{#if}}`` blocks:

    * ``include_day2_round1_results_block`` — turns on when
      ``day_number >= 2 and discussion_round == 1`` so the LLM is
      reminded to surface previous-night ability results in their
      first speech of the day.
    * ``include_day1_round1_wolf_madman_block`` — wolf/madman-only
      branch on day-1 round-1 that walks the 占い師騙り / 霊媒師騙り
      / 潜伏 triad. Other roles never see partner / fake-CO tactics.
    """
    return render_template(
        _TASK_DAYTIME_SPEECH_TEMPLATE,
        day_number=day_number,
        co_claim_options=format_co_claim_options(separator=" / "),
        include_day2_round1_results_block=(
            day_number >= 2 and discussion_round == 1
        ),
        include_day1_round1_wolf_madman_block=(
            day_number == 1
            and discussion_round == 1
            and role in (Role.WEREWOLF, Role.MADMAN)
        ),
    )


def task_vote(
    candidate_tokens: Sequence[str],
    runoff: bool,
    *,
    role: Role | None = None,
    wolf_partner_tokens: Sequence[str] = (),
) -> str:
    """Vote-phase task instruction.

    Body lives in ``master/task_vote.md``. Candidates are
    ``席{N} {display_name}`` tokens; target_name must echo one back.

    ``role`` + ``wolf_partner_tokens`` are an additive, wolf-only
    enrichment: the template's ``{{#if has_wolf_block}}`` branch
    appends a checklist that names the partner and walks 熟練狼's
    vote-discipline tradeoffs (身内票 / ライン切り / 票逸らしリスク
    / 決選投票). Other roles flip ``has_wolf_block`` off so partner
    identity and wolf-side voting tactics never reach non-wolf
    prompts.
    """
    has_wolf_block = role is Role.WEREWOLF and bool(wolf_partner_tokens)
    return render_template(
        _TASK_VOTE_TEMPLATE,
        runoff_note="これは決選投票です。" if runoff else "",
        names="、".join(candidate_tokens),
        has_wolf_block=has_wolf_block,
        partners="、".join(wolf_partner_tokens) if has_wolf_block else "",
        runoff=runoff and has_wolf_block,
    )


def task_night_action(kind: SubmissionType, candidate_tokens: Sequence[str]) -> str:
    """Night-action task instruction.

    Body lives in ``master/task_night_action.md``. Candidates are
    ``席{N} {display_name}`` tokens; target_name must echo one back.

    Three role-scoped advice paragraphs (wolf-attack value scoring,
    knight-guard tradeoffs, seer-divine target value) are gated by
    template ``{{#if}}`` blocks keyed off the action kind so each
    role only sees its own decision checklist.
    """
    label = {
        SubmissionType.WOLF_ATTACK: "襲撃",
        SubmissionType.SEER_DIVINE: "占い",
        SubmissionType.KNIGHT_GUARD: "護衛",
    }[kind]
    return render_template(
        _TASK_NIGHT_ACTION_TEMPLATE,
        label=label,
        names="、".join(candidate_tokens),
        is_wolf_attack=kind is SubmissionType.WOLF_ATTACK,
        is_knight_guard=kind is SubmissionType.KNIGHT_GUARD,
        is_seer_divine=kind is SubmissionType.SEER_DIVINE,
    )


def task_wolf_chat(partner_tokens: Sequence[str], candidate_tokens: Sequence[str]) -> str:
    """Wolf-chat coordination task instruction.

    Body lives in ``master/task_wolf_chat.md``. Asks a wolf to post a
    short coordination line to the wolves-only chat naming the
    intended attack target with concise reasoning.
    """
    return render_template(
        _TASK_WOLF_CHAT_TEMPLATE,
        partners="、".join(partner_tokens) if partner_tokens else "(なし)",
        names="、".join(candidate_tokens),
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
