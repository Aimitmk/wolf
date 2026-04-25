# `wolfbot` 2026-04-25 LLM 人狼投票判断強化プロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player が人狼のとき、相方の人狼が皆に怪しまれている局面で 1 人だけ投票先を逸らして人狼が透ける挙動を減らし、熟練した人狼プレイヤーらしく投票・身内票・ライン切り・決選投票を判断できるようにすることです。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player を、人狼役の昼投票判断においてより強い熟練者として振る舞わせる。
- 人狼 LLM が、相方の人狼が公開ログ上で強く疑われているときに、1 人だけ不自然に別の投票先へ逃げて自分まで透ける挙動を減らす。
- 人狼 LLM が、票筋、ライン、身内票、ライン切り、便乗、決選投票での切り捨てを盤面に応じて比較できるよう prompt を強化する。
- ただし投票ルール、候補生成、投票解決、DB schema、Discord UI は変更しない。

今回必ず対応すること:
1. 人狼 role-specific strategy に、相方が処刑濃厚なときの自然な投票判断を追加すること。
2. 人狼 LLM に、相方を露骨に庇う票逸らしが狼ラインを透かすリスクを明記すること。
3. 必要な局面では、相方への身内票・ライン切りを熟練者の選択肢として扱わせること。
4. ただし常に相方へ投票する固定ルールにはしないこと。相方を救える自然な対抗候補がある場合は、票合わせや誘導も選択肢に残す。
5. 投票 task (`task_vote`) で、人狼 voter にだけ相方情報を使った投票チェックリストを出すこと。
6. 非人狼 prompt、狂人 prompt、村役職 prompt に、人狼相方情報や人狼専用の投票戦術を漏らさないこと。
7. 既存の persona 話法、structured output、候補トークン完全一致、fire-and-forget LLM vote dispatch を壊さないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築、LLM 投票フロー、role-specific strategy、既存テストを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つこと。今回の変更で投票集計、同票処理、決選投票、状態遷移、勝敗判定を変えない。
- Discord channel history を直接読んで prompt に入れてはいけない。LLM 文脈は既存どおり DB の public/private logs からだけ構築すること。
- 現在の未公開投票状況を人狼 LLM に見せてはいけない。投票済み/未投票や投票先の途中経過を新しく prompt に渡さない。
- 人狼 LLM が見てよい秘匿情報は、自分の役職として見える相方情報と人狼専用 private log だけである。
- 狂人は本物の人狼位置を知らないため、狂人 prompt に相方投票・身内票・ライン切り実行指示を入れない。
- 実装後は必ず関連 pytest、ruff、mypy を走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/domain/enums.py`
- `src/wolfbot/domain/rules.py`
- `src/wolfbot/domain/state_machine.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`
- `tests/test_rules_votes.py`

このリポジトリで確認済みの事実:
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md`。
- system prompt は `src/wolfbot/llm/prompt_builder.py::build_system_prompt()` が合成する。
- 共通ルールは `src/wolfbot/llm/prompt_builder.py::_build_game_rules_block()` にある。
- 役職別の立ち回りは `_ROLE_STRATEGIES[role]` にあり、人狼向け strategy は `_ROLE_STRATEGIES[Role.WEREWOLF]` にある。
- 投票 task は `task_vote(candidate_tokens, runoff)` が作る。
- LLM 投票は `LLMAdapter._run_votes()` が `task_vote()` を渡して `_ask()` し、`target_name` を候補トークンから解決して `GameService.submit_vote()` に渡す。
- `LLMAdapter._run_votes()` の per-seat voter には `Player.role` があるため、人狼 voter の場合だけ投票 task に相方情報を渡せる。
- 人狼相方情報は `build_user_context()` 内で `me.role is Role.WEREWOLF` の場合だけ `## 仲間の人狼` として user context に入る。
- 共通ルールには既に `票筋`、`ライン`、`身内切り`、`PP`、`RPP` などの推理語彙がある。
- 現状の `task_vote()` は role-agnostic で、投票先候補と候補トークン完全一致の制約だけを主に伝える。
- 現状の人狼 strategy には「相方を露骨に庇いすぎない」趣旨はあるが、投票時に相方が処刑濃厚な場合の身内票・ライン切り・票逸らしリスクまでは十分に明示されていない。

実装方針:
- まずは prompt 強化で対応する。新しい DB カラム、投票 resolver、Discord UI、候補生成ロジックは追加しない。
- `build_system_prompt()` の公開引数は変えない。
- `build_user_context()` の秘匿範囲は変えない。
- `task_vote()` は backward compatible な optional 引数追加に留める。
- `LLMAction` schema は変更しない。`target_name` は引き続き候補トークン完全一致で返させる。
- 変更範囲は原則として `src/wolfbot/llm/prompt_builder.py`、`src/wolfbot/services/llm_service.py`、関連 tests に閉じる。

## 1. 人狼 strategy に投票 discipline を追加する

`_ROLE_STRATEGIES[Role.WEREWOLF]` に、以下の趣旨を必ず追加すること。

必須内容:
- 投票は発言と同じくらい強い情報になる。人狼は自分と相方の票筋が翌日以降に読まれることを前提に動く。
- 相方が公開ログ上で強く疑われ、処刑候補として自然に票が集まりそうなとき、自分だけ別の弱い候補へ投票すると「相方を庇った狼」に見られやすい。
- 相方を救えない局面では、身内票やライン切りで相方へ投票し、自分が翌日以降に残る価値を優先する。
- 相方へ投票する場合でも、唐突に切るのではなく、公開ログ上の発言・CO 矛盾・票筋・視点漏れなど、村人にも自然に見える理由を添える。
- 相方を救う場合は、自然に票が集まり得る対抗候補がいて、自分の過去発言・投票理由・騙り結果と矛盾しないときだけ検討する。
- 無理な票逸らし、根拠の薄い別候補投票、決選投票での露骨な相方庇いは、自分と相方のラインを濃くする。
- 決選投票では、相方を救う票が成功する見込み、失敗したときの自分の透けリスク、相方を切って自分が白く残る価値を比較する。
- 人狼 2 生存で PP/RPP が近い場合は、相方を切るより生存維持や票合わせが強い局面もあるため、縄数と勝ち筋を確認する。
- 「常に相方へ投票」でも「常に相方を庇う」でもなく、公開ログ上もっとも自然で勝ち筋が残る投票を選ぶ。

推奨文面例:

```text
- 投票は翌日以降に票筋として読まれる。相方が処刑濃厚な局面で自分だけ弱い別候補へ投票すると、相方を庇った狼として透けやすい。
- 相方を救えない局面では、身内票やライン切りで相方へ投票し、自分が白く残る価値を優先する。投票理由は公開ログ上の発言・CO 矛盾・票筋・視点漏れに沿って自然に作る。
- 相方を救う票は、自然に票が集まり得る対抗候補がいて、自分の過去発言や騙り結果と矛盾しない場合だけ検討する。無理な票逸らしは狼ラインを濃くする。
- 決選投票では、相方救済の成功見込み、自分の透けリスク、相方を切って翌日以降に残る価値、PP/RPP の近さを比較する。
```

注意:
- 人狼 strategy には `相方`、`身内票`、`ライン切り` などの人狼専用判断を書いてよい。
- `Role.WEREWOLF` 以外の strategy に、実際の相方がいる前提の投票戦術を入れないこと。
- 狂人 strategy には「票筋やラインを見る」程度の一般推理はあってよいが、本物の狼位置を知っている前提で身内票を指示しないこと。

## 2. `task_vote()` を人狼 voter だけ role-aware にする

`src/wolfbot/llm/prompt_builder.py::task_vote()` を、既存呼び出しを壊さない形で拡張すること。

推奨シグネチャ:

```python
def task_vote(
    candidate_tokens: Sequence[str],
    runoff: bool,
    *,
    role: Role | None = None,
    wolf_partner_tokens: Sequence[str] = (),
) -> str:
    ...
```

必須内容:
- 既存の `task_vote(candidate_tokens, runoff)` 呼び出しはそのまま動くこと。
- `role is Role.WEREWOLF` かつ `wolf_partner_tokens` が空でない場合だけ、人狼向けの投票判断チェックリストを追加する。
- 人狼向けチェックリストには、相方 token を明示してよい。これは人狼本人に既に見えている秘匿情報である。
- 非人狼 role、`role=None`、相方 token なしの場合は、現行と同じ一般投票 task に留める。
- 候補トークン完全一致、`intent=vote`、`target_name` の既存制約は変えない。
- `skip` はどうしても棄権したい場合だけにし、人狼が困ったから安易に `skip` しないようにする。

人狼向け task に必ず含める趣旨:
- 仲間の人狼: `席N Name` の一覧。
- 相方が候補に含まれ、公開ログ上で処刑濃厚なら、自分だけ不自然に票を逸らすと狼ラインが透ける。
- 相方を救えない局面では、身内票やライン切りを選択肢に入れる。
- 相方を救うなら、自然な対抗候補と公開ログ上の理由が必要。
- 決選投票では、救済成功見込み、自分の透けリスク、相方を切る価値、PP/RPP の近さを比較する。
- 最後は必ず合法候補 token から 1 名を返す。

推奨文面例:

```text
 あなたは人狼です。仲間の人狼: 席3 Alice。
 投票は翌日以降に票筋として読まれます。仲間が処刑濃厚な候補になっているとき、自分だけ弱い別候補へ投票すると庇いとして透けやすいです。
 仲間を救えない局面では身内票・ライン切りで仲間へ投票することも検討してください。救う場合は、自然に票が集まる対抗候補と公開ログ上の理由が必要です。
 決選投票では、仲間救済の成功見込み、自分の透けリスク、仲間を切って残る価値、PP/RPP の近さを比較してください。
```

注意:
- `task_vote()` に現在の未公開投票状況を渡さないこと。
- `task_vote()` は「疑われているか」を自動判定しない。LLM 自身が user context の公開ログを読んで判断する。
- 「相方が候補なら必ず投票」ではなく、「不自然な票逸らしを避け、盤面に応じて身内票も使う」指示にする。

## 3. `LLMAdapter._run_votes()` から人狼 voter の相方 token を渡す

`src/wolfbot/services/llm_service.py::LLMAdapter._run_votes()` の `_one_vote()` 内で、`task_vote()` 呼び出し前に人狼用の相方 token を作ること。

推奨実装:

```python
wolf_partner_tokens: list[str] = []
if voter.role is Role.WEREWOLF:
    wolf_partner_tokens = [
        seat_token(seats_by_no[p.seat_no])
        for p in all_players
        if p.alive
        and p.role is Role.WEREWOLF
        and p.seat_no != voter.seat_no
        and p.seat_no in seats_by_no
    ]

task_text = task_vote(
    [seat_token(c) for c in cand_seats],
    runoff=round_ == 1,
    role=voter.role,
    wolf_partner_tokens=wolf_partner_tokens,
)
```

必須内容:
- 人狼 voter 以外では `wolf_partner_tokens` を空にする。
- 死亡済み相方は投票対象ではないため、基本は alive partner だけでよい。
- 決選投票でも同じ task 拡張を使う。
- `cand_seats` の候補生成、既存 vote idempotency、stale phase guard、parallel dispatch は変えない。
- 相方 token が candidate_tokens に含まれない場合でも、「仲間の人狼」として表示してよいが、投票先は合法候補 token のみであることを維持する。

情報秘匿:
- `all_players` の実役職を使ってよいのは、投票者自身が人狼で相方を知っている場合だけ。
- 非人狼 voter の prompt に `Player.role` 由来の人狼位置を渡してはいけない。
- 狂人 voter には人狼位置を渡さない。

## 4. 情報秘匿と role leak を壊さない

必要な仕様:
- 非人狼 prompt に、`相方`、`仲間の人狼`、`身内票`、`ライン切りで相方へ投票` などの人狼専用実行指示が混ざってはいけない。
- 共通ルールの `身内切り` や `ライン` は公開ログを読むための一般語彙として残してよい。
- 人狼 strategy と人狼 vote task は、実際の相方を知っている人狼本人にだけ届く。
- `build_user_context()` の `wolf_partner_block` は既存どおり `me.role is Role.WEREWOLF` の場合だけ表示する。
- `task_vote()` の optional 引数を追加しても、既存 tests や外部呼び出しを壊さない。

やってはいけないこと:
- 投票 resolver や `compute_vote_result()` を変更する。
- 投票候補から相方を自動除外または自動選択する。
- LLM の投票先を code 側で強制的に相方へ変える。
- 途中投票状況を LLM prompt に渡す。
- 公開ログ parser や疑われ度スコアを新規実装する。
- DB schema、Discord command、DM vote UI を変更する。
- `LLMAction` schema を変更する。
- 非狼 role に相方情報を渡す。
- 狂人に本物の人狼位置が見えている前提の文面を書く。
- 無関係な refactor。

## 5. テストを追加 / 更新する

`tests/test_llm_prompt_builder.py`:
- `_build_strategy_block(Role.WEREWOLF)` に `身内票`、`ライン切り`、`票筋`、`処刑濃厚`、`票を逸らす`、`決選投票` が含まれること。
- `_build_strategy_block(Role.WEREWOLF)` に、相方を救えない局面では相方へ投票して自分が残る価値を優先する趣旨が含まれること。
- `_build_strategy_block(Role.WEREWOLF)` に、相方を救う場合は自然な対抗候補と公開ログ上の理由が必要という趣旨が含まれること。
- `Role.MADMAN`、`Role.SEER`、`Role.MEDIUM`、`Role.KNIGHT`、`Role.VILLAGER` の strategy に、人狼専用の `相方`、`仲間の人狼`、`相方を救う` などが漏れないこと。
- `task_vote(["席1 A", "席2 B"], runoff=False, role=Role.WEREWOLF, wolf_partner_tokens=["席2 B"])` に、人狼向け投票 discipline が含まれること。
- 人狼向け `task_vote()` に `身内票`、`ライン切り`、`票筋`、`処刑濃厚`、`合法候補` が含まれること。
- `task_vote(["席1 A", "席2 B"], runoff=False)` の通常呼び出しには、人狼専用語彙が含まれないこと。
- `task_vote(..., role=Role.VILLAGER)` や `role=Role.MADMAN` には、人狼専用相方投票 guidance が含まれないこと。
- 決選投票 (`runoff=True`) でも人狼向け guidance に `決選投票`、`透けリスク`、`PP/RPP` が含まれること。

`tests/test_llm_service.py`:
- `_CapturingDecider` または既存の system prompt capture helper を使い、LLM 人狼の vote task 経由で `system_prompt` に新しい人狼投票 guidance が届くことを検証する。
- 具体的には、Role.WEREWOLF の vote prompt に `仲間の人狼`、`身内票`、`ライン切り`、`票筋`、`透け` が含まれること。
- 非人狼 LLM の vote prompt に `仲間の人狼`、`身内票`、`ライン切りで相方` が入らないことを検証する。
- 狂人 LLM の vote prompt に本物の人狼位置や相方 token が入らないことを検証する。
- 決選投票 (`round_=1`) の人狼 vote prompt にも同じ guidance が届くことを検証する。
- 既存の parallel vote dispatch、idempotency、stale phase guard、target resolver tests を壊さないこと。

既存テスト群は壊さないこと:
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`
- `tests/test_rules_votes.py`

## 6. 受け入れ条件

- 人狼 LLM は、相方が強く疑われている局面で、自分だけ不自然に票を逸らすリスクを prompt 上で理解できる。
- 人狼 LLM は、相方を救えない局面では身内票・ライン切りを選択肢に入れられる。
- 人狼 LLM は、相方を救う場合も、自然な対抗候補・公開ログ上の理由・過去発言との整合を必要条件として考える。
- 決選投票では、相方救済の成功見込み、自分の透けリスク、相方を切って残る価値、PP/RPP の近さを比較できる。
- 非人狼 LLM と狂人 LLM に、実際の人狼相方情報や人狼専用投票 guidance が漏れない。
- 投票ルール、投票候補、投票解決、同票/決選処理、DB schema、Discord UI、structured output schema は変わらない。
- LLM の `target_name` は引き続き合法候補 token と完全一致する。
- 人狼の投票先を code で強制するのではなく、prompt による判断改善として実装されている。

## 7. 検証コマンド

最低限:

```bash
uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py
uv run ruff check src tests
uv run mypy
```

可能なら関連範囲も走らせる:

```bash
uv run pytest tests/test_llm_structured_output.py tests/test_llm_resolver.py tests/test_llm_trigger.py tests/test_rules_votes.py
```

最後に簡潔に報告すること:
- どの prompt 文面を強化したか
- `task_vote()` と `LLMAdapter._run_votes()` の変更概要
- 人狼専用情報が非狼 prompt に漏れないようにした境界
- 実行したテスト / lint / 型チェックと結果
- 残課題があればその内容
```
