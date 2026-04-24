# `wolfbot` 2026-04-24 LLM Fake Seer Random White Guard Prompt

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player、とくに人狼・狂人が初日に占い師を騙るとき、この bot の `NIGHT_0` ランダム白仕様を忘れて、存在しない初日黒結果を主張しないようにすることです。目的は、LLM player を強い熟練した人狼プレイヤーに近づけつつ、実装ルールと矛盾しない騙りをさせることです。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player を、より強い熟練した 9 人村プレイヤーに近づける。
- 特に、人狼・狂人が day 1 に占い師騙りをする場合、この bot の `NIGHT_0` ランダム白仕様に合わせて初回結果を必ず白にし、初日から黒結果を捏造しないよう system prompt / role strategy を更新する。
- 変更は LLM prompt / strategy / prompt test 周辺に閉じる。ゲームルール、DB schema、Discord command、状態遷移、権限管理、復旧処理は変えない。

今回必ず対応すること:
1. 共通ルールまたは偽 CO ルールに、「この bot では初回の占い結果は `NIGHT_0` ランダム白であり、本物の人狼ではない相手への白結果として扱われる」と明記すること。
2. 人狼・狂人の占い師騙り strategy に、「day 1 に占い師 CO する場合、初回結果はランダム白に相当する白結果として出し、初日黒を出さない」と明記すること。
3. 人狼・狂人の占い師騙り strategy に、「黒出しは day 2 以降、公開ログ・判定時系列・生存状況・過去の自分の主張と矛盾しない場合だけ検討する」と明記すること。
4. 狂人 strategy では、狂人は本物の人狼位置を知らないため、黒出しには誤爆リスクがあることを維持・補強すること。
5. 人狼 strategy では、相方を知っている立場でも、初日ランダム白仕様と公開ログの時系列に反する結果を出すと破綻する、と明記すること。
6. 既存の情報秘匿、role-specific strategy の分離、persona 話法 block、structured output 制約を壊さないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築とテストを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つこと。今回の変更でルールエンジンや状態遷移を変えない。
- LLM prompt 構築は `src/wolfbot/llm/prompt_builder.py` と `src/wolfbot/prompts/llm_system_prompt.md` 周辺に閉じること。
- `src/wolfbot/prompts/llm_system_prompt.md` の基本構造は維持する。共通ルールは `{game_rules_block}`、役職固有戦略は `{strategy_block}`、出力制約や絶対ルールは template に置く。
- user context に CO parser、自動盤面分類、縄数自動計算、役職推定結果を足さない。今回は LLM が公開ログを読むための判断方針を明文化する。
- Discord channel history を直接拾って prompt に入れてはいけない。LLM 文脈は既存どおり DB の public/private logs からだけ構築すること。
- `game_id` / `audience_seat` ベースの情報分離を壊さないこと。
- DB schema は変更しないこと。
- slash command は追加しないこと。
- 既存の advance loop、`WAITING_HOST_DECISION`、recovery、fire-and-forget の設計を壊さないこと。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/llm/personas.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/domain/enums.py`
- `src/wolfbot/domain/rules.py`
- `src/wolfbot/domain/state_machine.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`

このリポジトリで確認済みの事実:
- `src/wolfbot/llm/prompt_builder.py` には、すでに `_build_game_rules_block()` と `_build_strategy_block(role)` がある。
- `src/wolfbot/prompts/llm_system_prompt.md` には、共通ルール、人格、話法、自分の役職、役職別の立ち回り指針、現在フェイズ、今回タスクの block がある。
- `_build_game_rules_block()` には、`NIGHT_0` に占い師へ提示されるランダム白は本物の人狼ではない相手が選ばれる、という共通ルールがすでにある。
- `domain/rules.py::is_detected_as_wolf(role)` は `Role.WEREWOLF` だけを黒扱いする。狂人は占い・霊媒で白に出る。
- `build_system_prompt()` は seat ごと・呼び出しごとに system prompt を組み立てている。
- `LLMAdapter._ask()` は `repo.load_public_logs(game.id, ...)` と `repo.load_private_logs_for_audience(game.id, audience_seat=player.seat_no, ...)` を使っており、DB 読み出しは `game_id` 単位にスコープされている。
- `build_user_context()` では、人狼相方情報は `me.role is Role.WEREWOLF` の場合だけ user context に入る。
- 既存テストは、共通ルール block、役職別 strategy block、話法 block、role leak 防止をすでに検証している。
- 現在の人狼・狂人 strategy には、day 1 の占い師騙り、条件付き騙り、誤爆リスク、騙りすぎ警戒はあるが、「day 1 占い師騙りの初回結果はランダム白として白にする。初日黒は出さない」という実戦上重要な制約が十分に固定されていない。

実装要求

## 1. 共通ルール block の偽占い結果ルールを補強する

必要な仕様:
- すべての LLM player が、真占い師・偽占い師を問わず、この bot の初回占い結果の扱いを理解すること。
- 真占い師に提示される `NIGHT_0` ランダム白は、本物の人狼ではない相手への白結果であること。
- 人狼・狂人が占い師騙りをする場合でも、day 1 に名乗る初回結果はこのランダム白仕様と矛盾しない白結果にすること。
- day 1 の占い師 CO で「初日に黒を引いた」と主張すると、この bot の実ルール上の時系列と矛盾するため破綻要素になる、と明記すること。

このタスクで固定する実装方針:
- `src/wolfbot/llm/prompt_builder.py` の `_build_game_rules_block()` に、偽占い師が初回結果を作る際の制約を追加する。
- 既存の `NIGHT_0` ランダム白ルールを削らず、その直後または偽 CO ルール節に追記する。
- `src/wolfbot/prompts/llm_system_prompt.md` の構造は大きく変えなくてよい。既存の `{game_rules_block}` に含める形を優先する。
- user context に新しい集計データを足さないこと。今回は prompt 文面の強化に留める。

共通ルール block に必ず含める内容:
- 「`NIGHT_0` ランダム白は初回占い結果として扱う」
- 「day 1 に占い師 CO する真占い師は、そのランダム白を初回白結果として公開できる」
- 「人狼・狂人が day 1 に占い師騙りをする場合も、初回結果は白として主張する」
- 「day 1 の占い師騙りで初回黒を主張しない」
- 「黒結果は day 2 以降、夜の占いが発生した想定と時系列が合う場合だけ主張できる」

推奨文面例:
- `NIGHT_0 のランダム白は、day 1 に公開できる初回占い結果として扱う。day 1 に占い師 CO する場合、真占い師も偽占い師も初回結果は白結果として時系列を合わせる。`
- `人狼・狂人が day 1 に占い師騙りをする場合、初日黒を主張しない。この bot の初回結果はランダム白なので、day 1 の黒結果主張は破綻要素になる。`
- `黒結果を騙るなら、day 2 以降に夜の占いがあった想定で、公開ログ・生存状況・過去の自分の判定履歴と矛盾しない場合だけ検討する。`

## 2. 人狼 strategy を「初日騙り白結果」前提に更新する

必要な仕様:
- 人狼の day 1 占い師騙りは引き続き有効な選択肢だが、初回結果は必ず白として組み立てる。
- 初日黒出しは、吊りやすさ以前にこの bot の初回占い仕様と矛盾するため避ける。
- 白先は、公開ログ上の発言・相方位置・囲いリスク・噛み筋予定と整合する相手を選ぶ。
- 相方を白で囲う場合は、発言・投票・噛み筋と整合する理由を用意する。露骨な囲いはラインを疑われる。
- 黒出しは day 2 以降、霊媒結果・投票・襲撃結果・対抗 CO まで見て、破綻しない場合だけ検討する。

このタスクで固定する実装方針:
- `_ROLE_STRATEGIES[Role.WEREWOLF]` を更新する。
- 既存の人狼専用語彙 `相方` / `襲撃先を揃える` は人狼 strategy のみに残し、他役職へ漏らさない。
- 人狼 strategy に入れる内容は人狼本人向けなので、相方連携や囲い方針を書いてよい。

人狼 strategy に必ず含める内容:
- 「day 1 に占い師騙りをする場合、初回結果はランダム白に合わせて白を出す」
- 「day 1 の初回黒はこの bot の実ルール上の破綻要素なので出さない」
- 「黒出しは day 2 以降に検討する」
- 「白先選びは、相方の位置、囲いリスク、公開ログ、投票、噛み筋と整合させる」
- 「相方を囲う場合は露骨になりすぎない」

推奨文面例:
- `day 1 に占い師騙りをする場合、初回結果は NIGHT_0 ランダム白に合わせて白を出す。初日黒はこの bot の実ルール上の時系列と矛盾するため主張しない。`
- `白先は、相方を守るか、白く見える非 CO 位置を利用するか、噛み筋と整合するかを見て選ぶ。相方を囲うなら発言・投票・噛み筋に理由を用意する。`
- `黒出しは day 2 以降、霊媒結果・投票・襲撃結果・対抗 CO の反応まで考えて、破綻しにくい場合だけ検討する。`

## 3. 狂人 strategy を「初日騙り白結果」前提に更新する

必要な仕様:
- 狂人の day 1 占い師騙りは引き続き強い基本候補だが、初回結果は必ず白として組み立てる。
- 狂人は本物の人狼位置を知らない。白先が本物の狼とは限らないし、黒先が本物の狼である誤爆リスクもある。
- 初日黒は、誤爆リスク以前にこの bot の初回占い仕様と矛盾するため避ける。
- day 1 の白出しでは、真占い師の確定を防ぎ、議論を割り、白先の反応から支援先を調整する。
- 黒出しは day 2 以降、誤爆リスクと破綻リスクを見て限定的に検討する。

このタスクで固定する実装方針:
- `_ROLE_STRATEGIES[Role.MADMAN]` を更新する。
- 狂人 strategy に `相方` / `襲撃先を揃える` のような人狼専用連携語彙を入れない。
- 狂人 strategy は「狼位置を知らないが、村の情報整理を乱すために騙る」という既存方針を維持する。

狂人 strategy に必ず含める内容:
- 「day 1 に占い師騙りをする場合、初回結果はランダム白に合わせて白を出す」
- 「day 1 の初回黒はこの bot の実ルール上の破綻要素なので出さない」
- 「黒出しは day 2 以降に検討する」
- 「狂人は本物の人狼位置を知らないため、黒出しには誤爆リスクがある」
- 「白先が本物の狼とは限らないため、白先を確定の味方として扱わない」

推奨文面例:
- `day 1 に占い師騙りをする場合、初回結果は NIGHT_0 ランダム白に合わせて白を出す。初日黒はこの bot の実ルール上の時系列と矛盾するため主張しない。`
- `狂人は本物の人狼位置を知らない。白先が狼とは限らず、黒出しは day 2 以降でも誤爆リスクがあるため、公開ログ上の反応を見て慎重に選ぶ。`
- `白出しは破綻しにくく、真占い師の確定を防ぎ、議論を割る材料になる。ただし白先を確定の味方として扱わない。`

## 4. 偽 CO の結果作成ルールを時系列重視にする

必要な仕様:
- 人狼・狂人が偽 CO する場合でも、公開ログ、処刑結果、夜結果、死亡者、合法対象、過去発言と矛盾しない。
- 偽占い師は、初回結果・以後の結果を日付順に管理する。
- day 1 の初回結果は白、day 2 以降の結果は前夜に占った想定の対象への結果として扱う。
- 偽占い師は、死亡済み対象や、過去に自分が出した判定と矛盾する結果を安易に作らない。
- 偽霊媒師・偽騎士の既存ルールは維持する。

このルールは次のどちらかに入れる:
- 共通ルールの「嘘をつける役職でも破綻を避ける」節
- または werewolf / madman strategy の両方

共通に入れる場合でも、狼相方や襲撃先調整などの人狼専用情報は書かない。

## 5. 情報秘匿と prompt 分離を維持する

必要な仕様:
- 非人狼の prompt に狼相方情報が混ざってはいけない。
- 狼以外の role-specific strategy に、人狼専用の夜連携戦術が混ざってはいけない。
- 狂人には真の人狼位置を知らせない。
- 別 `game_id` の log は current game の prompt に混ざってはいけない。
- Discord の message history を直接拾って prompt に入れてはいけない。

このタスクで固定する作業:
- `LLMAdapter._ask()` の DB ベースの文脈構築は維持する。
- `build_user_context()` の現在のスコープ分離は壊さない。
- system prompt と role strategy の文面だけを強化し、role leak が起きないようにする。

やってはいけないこと:
- 配役を変える
- ルールエンジンを変える
- 状態遷移を変える
- DB schema を増やす
- slash command を増やす
- Discord API から message history を直接 prompt に流す
- 非狼に相方情報を見せる
- 狂人に本物の狼位置が見えている前提で書く
- 村人 / 占い師 / 霊媒師 / 騎士に、人狼専用の相方連携 strategy を見せる
- 無関係な refactor を広げる

必要なテスト変更:

## `tests/test_llm_prompt_builder.py` を更新する
- `_build_game_rules_block()` に、`NIGHT_0` ランダム白が day 1 の初回占い結果であることが含まれること。
- `_build_game_rules_block()` に、人狼・狂人が day 1 に占い師騙りをする場合も初回結果は白にする趣旨が含まれること。
- `_build_game_rules_block()` に、day 1 の初回黒主張が破綻要素になる趣旨が含まれること。
- `_build_strategy_block(Role.WEREWOLF)` に、day 1 占い師騙りの初回白、初日黒禁止、day 2 以降の黒出し検討、白先選びの整合性が含まれること。
- `_build_strategy_block(Role.MADMAN)` に、day 1 占い師騙りの初回白、初日黒禁止、day 2 以降の黒出し検討、誤爆リスクが含まれること。
- `_build_strategy_block(Role.MADMAN)` に、`相方` / `襲撃先を揃える` などの wolf-only vocabulary が含まれないこと。
- 既存の cross-role leak テストを維持し、新しい文面で落ちる場合は、仕様を満たす形で期待値を更新すること。

## `tests/test_llm_service.py` を更新する
- `_CapturingDecider` を使い、`_ask()` が組み立てた `system_prompt` に `NIGHT_0` ランダム白と day 1 初回白ルールが届くことを検証する。
- 人狼 seat の `system_prompt` に、day 1 占い師騙りの初回白・初日黒禁止が含まれることを検証する。
- 狂人 seat の `system_prompt` に、day 1 占い師騙りの初回白・初日黒禁止・誤爆リスクが含まれ、かつ人狼専用連携語彙が含まれないことを検証する。
- 非狼の prompt に狼相方情報や狼専用 strategy が入らない既存保証を維持する。

既存テスト群は壊さないこと:
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`

受け入れ条件:
- 人狼 / 狂人 LLM が day 1 に占い師騙りをする場合、初回結果として白を出す prompt になっている。
- 人狼 / 狂人 LLM が day 1 に存在しない初回黒を主張しない prompt になっている。
- 黒出しは day 2 以降、公開ログ・判定履歴・生存状況・霊媒結果・投票・襲撃結果と矛盾しない場合だけ検討する指針になっている。
- 狂人 LLM は、本物の人狼位置を知らないため黒誤爆リスクを認識する。
- 人狼 LLM は、相方を知っていても初回占い仕様と公開時系列に反する結果を出さない。
- user context の秘匿範囲を広げず、system prompt / role strategy 強化だけで判断方針を改善する。

実行する検証コマンド:
- `uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py tests/test_llm_structured_output.py tests/test_llm_resolver.py tests/test_llm_trigger.py`
- `uv run ruff check src tests`
- `uv run mypy`

最後に簡潔に報告すること:
- 何を変えたか
- どのファイルを変えたか
- 実行した検証コマンドと結果
- 残課題があればその内容
```
