# `wolfbot` 2026-04-28 LLM day 1 霊媒COタイミング熟練者化プロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player を強い熟練した 9 人村プレイヤーとして振る舞わせることです。特に day 1 の 1 巡目に、人狼・狂人 LLM が不用意に霊媒騙りへ出たり、真霊媒師 LLM が早すぎる CO をしたりする動きを抑え、1 巡後の `2-0` / `2-1` 盤面を読んだ自然な霊媒 CO タイミングへ寄せます。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player を、より強い熟練した 9 人村プレイヤーとして振る舞わせる。
- 人狼・狂人 LLM が day 1 の 1 巡目から霊媒騙りをして不自然に CO 数を増やす動きを避ける。
- 人狼・狂人 LLM が、1 巡後に `2-0` 盤面が確定し、自分がグレー位置の場合だけ、2 巡目で投票位置を狭めるための自然な霊媒騙りを検討できるようにする。
- 人狼・狂人 LLM が、2 巡目で `2-1` 盤面が確定した場合も、対抗霊媒として「出ざるを得ない」形の自然な霊媒騙りを検討できるようにする。
- 真霊媒師 LLM が day 1 の 1 巡目では CO せず、1 巡後の `2-0` 盤面で自分がグレーなら 2 巡目に CO し、霊媒騙りが出た場合は当然対抗 CO するようにする。
- 変更は LLM prompt / prompt test 周辺に閉じる。ゲームルール、DB schema、Discord command、状態遷移、LLM structured output schema は変えない。

今回必ず対応すること:
1. 人狼・狂人 strategy から、day 1 の 1 巡目に霊媒師騙りを選択肢として促す文面を外すこと。
2. 人狼・狂人 strategy に、day 1 の 1 巡目は霊媒騙りをせず、占い師騙りまたは潜伏を中心に比較する方針を入れること。
3. 人狼・狂人 strategy に、day 1 の 2 巡目で `2-0` 盤面が確定し、自分がグレー位置なら、投票候補を狭める自然な霊媒 CO として霊媒騙りを検討する方針を入れること。
4. 人狼・狂人 strategy に、day 1 の 2 巡目で `2-1` 盤面が確定した場合も、対抗霊媒として「出ざるを得ない」自然な霊媒 CO を検討してよい方針を入れること。
5. 真霊媒師 strategy に、day 1 の 1 巡目は CO せず、1 巡後に `2-0` 盤面が確定し、自分がグレー位置なら 2 巡目で CO する方針を入れること。
6. 真霊媒師 strategy に、day 1 の 2 巡目で霊媒騙りが出た場合は当然対抗 CO する方針を入れること。
7. `task_daytime_speech()` の day 1 role-aware guidance を更新し、現在の「1 巡目で占い師騙り・霊媒師騙り・潜伏の 3 択を比較」系の文面を、新しい 1 巡目 / 2 巡目方針に置き換えること。
8. 既存の 3 占い CO 盤面での追加霊媒・騎士騙り抑止、2-2 / 1-2 進行理解、偽結果整合、狂人の狼位置不知、role-specific strategy 分離を壊さないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築、role strategy、昼発言 task、LLM discussion flow、関連テストを把握してから修正すること。
- 作業前に `git status --short` を確認し、既存の未コミット変更を巻き戻さないこと。
- `domain/` は純粋ロジックのまま保つ。今回の変更でルールエンジンや状態遷移を変えない。
- DB schema は変更しない。
- slash command は追加しない。
- LLMAction / structured output schema は変更しない。
- Discord channel history を直接読んで prompt に入れてはいけない。LLM 文脈は既存どおり DB の public/private logs からだけ構築する。
- user context に新しい CO parser、自動盤面分類、役職推定表を追加しない。今回は system prompt / task text の戦略明文化で対応する。
- 特定の LLM にコードで霊媒 CO を強制しない。公開ログを読んだ LLM が条件付きで選ぶ prompt にする。
- 非人狼 prompt に、人狼・狂人向けの霊媒騙り実行指示を漏らしてはいけない。
- 狂人 prompt に、本物の人狼位置を知っている前提を書いてはいけない。
- 人狼・狂人の偽 CO strategy を全体として弱体化しすぎない。今回抑止するのは「day 1 の 1 巡目に霊媒騙りへ出る」動きであり、day 1 2 巡目の条件付き霊媒騙り、day 2 以降の偽結果提示、占い師騙り、潜伏、投票誘導、襲撃方針は維持する。
- 無関係な大規模 refactor をしない。
- 実装後は必ず関連テスト、ruff、mypy を走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/domain/enums.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`

このリポジトリで確認済みの事実:
- ゲームは 9 人村固定で、配役は `人狼2 / 狂人1 / 占い師1 / 霊媒師1 / 騎士1 / 村人3`。
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md`。
- system prompt は `src/wolfbot/llm/prompt_builder.py::build_system_prompt()` が seat ごとに合成する。
- 共通ルールは `src/wolfbot/llm/prompt_builder.py::_build_game_rules_block()` にある。
- 役職別の立ち回りは `src/wolfbot/llm/prompt_builder.py::_ROLE_STRATEGIES` にある。
- 昼発言 task は `task_daytime_speech()` にあり、`day_number`、`discussion_round`、`role` を受け取れる。
- `LLMAdapter._do_one_discussion_speech()` は `task_daytime_speech(game.day_number, discussion_round=..., role=player.role)` を渡している。
- `LLMAdapter._run_discussion_rounds()` は day discussion で LLM を席順に 2 巡させる。
- 既存の人狼・狂人 strategy には day 1 霊媒師騙り、2-2 / 1-2 盤面形成、day 2 以降の霊媒結果提示、3 占い CO 盤面で追加霊媒・騎士騙りをしない方針がある。
- 現在の `task_daytime_speech()` には、day 1 の 1 巡目で人狼・狂人へ「占い師騙り・霊媒師騙り・潜伏の 3 択」を比較させる文面がある。この文面は今回の要求と矛盾するため置き換える。
- 真霊媒師 strategy には、処刑翌日の結果公開、対抗霊媒が出た場合の対応、占い師 CO 処刑後の霊媒白の読み方などがある。
- `tests/test_llm_prompt_builder.py` と `tests/test_llm_service.py` は、role strategy と system prompt に重要文言が届くことを文字列断片で固定している。今回の方針変更に合わせて既存テストを更新する必要がある。

実装方針:
- 基本は `src/wolfbot/llm/prompt_builder.py` の文面更新で対応する。
- `src/wolfbot/prompts/llm_system_prompt.md` の大構造は変えない。既存の `{strategy_block}` と `{task_block}` の差し込み構造を使う。
- `Role.WEREWOLF` / `Role.MADMAN` strategy と、day 1 の `task_daytime_speech()` role-aware guidance を整合させる。
- `Role.MEDIUM` strategy と、必要なら `task_daytime_speech()` の `role is Role.MEDIUM` 向け guidance を追加して、真霊媒師の 1 巡目潜伏 / 2 巡目条件付き CO を明確にする。
- 盤面判定は LLM が公開ログから読む。コードで `2-0` / `2-1` を計算して分岐しない。
- 追加文面は、長い内部思考をそのまま発話させるのではなく、熟練者が判断軸として使う短い指針にする。

## 1. 人狼 strategy の day 1 霊媒騙りタイミングを更新する

`_ROLE_STRATEGIES[Role.WEREWOLF]` を更新し、以下の趣旨を必ず入れること。

必要な仕様:
- day 1 の 1 巡目では霊媒師騙りをしない。
- day 1 の 1 巡目では、占い師騙りに出るか、潜伏して白さを取るかを中心に比較する。
- 霊媒師騙りは day 1 から完全禁止ではなく、1 巡後の盤面を見た 2 巡目の条件付き選択肢にする。
- 1 巡後に占い師 CO が 2 人、霊媒師 CO が 0 人の `2-0` 盤面が確定し、自分が能力役職 CO していないグレー位置なら、2 巡目で霊媒師騙りを検討する。
- この霊媒騙りは、投票候補になり得るグレーを狭めるために自然に出た霊媒 CO に見せる。
- 2 巡目で占い師 CO が 2 人、霊媒師 CO が 1 人の `2-1` 盤面が確定した場合も、対抗霊媒として「出ざるを得ない」形なら霊媒騙りを検討してよい。
- `2-1` で対抗霊媒として出る場合は、真霊媒を単独進行役にしない、霊媒ローラーや霊媒比較に持ち込む、という価値を比較する。
- day 1 の霊媒師騙りでは、まだ処刑が発生していないので霊媒結果を出さない。「霊媒師として出る」「明日から処刑結果を見る」とだけ述べる。
- 人狼本体が霊媒ローラーで失われるリスク、相方の位置、占い騙りの有無、残り縄、PP/RPP を比較し、常に霊媒師騙りを選ぶ固定行動にはしない。

推奨文面例:
- `day 1 の 1 巡目では霊媒師騙りをしない。まず占い師騙りに出るか、潜伏して白さを取るかを中心に比較する。霊媒騙りは 1 巡後の盤面を見た 2 巡目の条件付き選択肢にする。`
- `day 1 の 2 巡目で、1 巡後に 2-0 盤面 (占い師 CO 2 人 + 霊媒師 CO 0 人) が確定し、自分が能力役職 CO していないグレー位置なら、霊媒師騙りを検討する。投票候補を狭めるために自然に出た霊媒 CO に見せやすい。`
- `day 1 の 2 巡目で 2-1 盤面 (占い師 CO 2 人 + 霊媒師 CO 1 人) が確定した場合も、対抗霊媒として出ざるを得ない形なら霊媒師騙りを検討してよい。単独霊媒を進行役にせず、霊媒比較や霊媒ローラーに持ち込む価値を比較する。`

人狼 strategy に含めるべき文言の要素:
- `day 1 の 1 巡目では霊媒師騙りをしない`
- `2 巡目`
- `2-0`
- `占い師 CO 2 人`
- `霊媒師 CO 0 人`
- `自分がグレー位置`
- `投票候補`
- `自然に出た霊媒 CO`
- `2-1`
- `対抗霊媒`
- `出ざるを得ない`
- `相方`

注意:
- 人狼 strategy には `相方` を書いてよい。
- 既存の 3 占い CO 盤面で「霊媒師騙りや騎士騙りを追加しない」方針は維持する。

## 2. 狂人 strategy の day 1 霊媒騙りタイミングを更新する

`_ROLE_STRATEGIES[Role.MADMAN]` を更新し、以下の趣旨を必ず入れること。

必要な仕様:
- day 1 の 1 巡目では霊媒師騙りをしない。
- day 1 の 1 巡目では、占い師騙りに出るか、潜伏して議論を歪めるかを中心に比較する。
- 霊媒師騙りは、1 巡後の盤面を見た 2 巡目の条件付き選択肢にする。
- 1 巡後に `2-0` 盤面が確定し、自分がグレー位置なら、2 巡目で霊媒師騙りを検討する。
- この霊媒騙りは、投票候補を狭めるために自然に出た霊媒 CO に見せる。
- 2 巡目で `2-1` 盤面が確定した場合も、対抗霊媒として「出ざるを得ない」形なら霊媒師騙りを検討してよい。
- 狂人は本物の狼位置を知らないため、霊媒騙りでグレーを狭めると本物の人狼を処刑候補に近づける危険がある。このリスクも比較する。
- day 1 の霊媒師騙りでは、まだ処刑が発生していないので霊媒結果を出さない。
- 常に霊媒師騙りを選ぶ固定行動にはせず、占い師 CO 数、霊媒師 CO 数、縄数、公開ログ上の信用差で選ぶ。

推奨文面例:
- `day 1 の 1 巡目では霊媒師騙りをしない。占い師騙りに出るか、潜伏して議論を歪めるかを中心に比較し、霊媒騙りは 1 巡後の盤面を見た 2 巡目の条件付き選択肢にする。`
- `day 1 の 2 巡目で 2-0 盤面が確定し、自分がグレー位置なら、投票候補を狭めるために自然に出た霊媒 CO として霊媒師騙りを検討する。`
- `day 1 の 2 巡目で 2-1 盤面が確定した場合も、対抗霊媒として出ざるを得ない形なら霊媒師騙りを検討してよい。ただし狂人は本物の狼位置を知らないため、グレーを狭めて人狼を処刑候補に近づける危険を必ず見る。`

狂人 strategy に含めるべき文言の要素:
- `day 1 の 1 巡目では霊媒師騙りをしない`
- `2 巡目`
- `2-0`
- `自分がグレー位置`
- `投票候補`
- `自然に出た霊媒 CO`
- `2-1`
- `対抗霊媒`
- `出ざるを得ない`
- `本物の狼位置を知らない`

注意:
- 狂人 strategy に `相方`、`襲撃先を揃える`、本物の人狼位置を知っている前提を書かない。
- `相方候補` は公開ログからの推理語彙としてなら使ってよいが、実際の人狼を知っているように書かない。

## 3. 真霊媒師 strategy の day 1 CO タイミングを更新する

`_ROLE_STRATEGIES[Role.MEDIUM]` を更新し、以下の趣旨を必ず入れること。

必要な仕様:
- day 1 の 1 巡目では CO しない。
- 1 巡後に `2-0` 盤面が確定し、自分が能力役職 CO していないグレー位置なら、day 1 の 2 巡目で霊媒 CO する。
- この CO は、投票候補となるグレーを狭めるための村利ある自然な霊媒 CO である。
- day 1 の 2 巡目で霊媒騙りが出た場合は、真霊媒師として当然対抗 CO する。
- 対抗 CO では、自分が真霊媒師であること、まだ処刑が発生していないため霊媒結果はないこと、翌日以降は処刑者への結果を出すことを短く述べる。
- day 1 なので存在しない霊媒結果を捏造しない。
- `2-0` でない、グレーではない、2 巡目ではない、または CO で投票候補を狭める効果が薄い場合は通常の潜伏方針を比較する。
- day 2 以降の処刑翌日は、既存方針どおり霊媒結果を公開して議論の軸を作る。

推奨文面例:
- `day 1 の 1 巡目では霊媒 CO しない。まず占い師 CO 数とグレーの狭まり方を見る。`
- `day 1 の 2 巡目で、1 巡後に 2-0 盤面 (占い師 CO 2 人 + 霊媒師 CO 0 人) が確定し、自分がグレー位置なら霊媒 CO する。投票候補を狭め、村に自然な進行軸を作る価値が高い。`
- `day 1 の 2 巡目で霊媒騙りが出た場合は、真霊媒師として当然対抗 CO する。まだ処刑がないため霊媒結果はなく、翌日以降に処刑者への結果を出すと明言する。`

霊媒師 strategy に含めるべき文言の要素:
- `day 1 の 1 巡目では霊媒 CO しない`
- `2 巡目`
- `2-0`
- `占い師 CO 2 人`
- `霊媒師 CO 0 人`
- `自分がグレー位置`
- `投票候補を狭め`
- `霊媒騙りが出た場合`
- `当然対抗 CO`
- `まだ処刑がないため霊媒結果はない`

注意:
- 真霊媒師 strategy に、人狼・狂人向けの `霊媒師騙り` を「自分が実行する」文面として入れない。
- 真霊媒師は「霊媒騙りが出た場合に対抗 CO する」と表現する。

## 4. `task_daytime_speech()` の day 1 role-aware guidance を更新する

`src/wolfbot/llm/prompt_builder.py::task_daytime_speech()` を更新する。

現在ある可能性が高い問題文面:
- `day 1 の 1 巡目で偽 CO を選ぶ場合、占い師騙り・霊媒師騙り・潜伏の 3 択を比較してください。`

この趣旨は今回の要求と矛盾するため、次の方針へ置き換えること。

人狼・狂人向け:
- `day_number == 1 and discussion_round == 1 and role in (Role.WEREWOLF, Role.MADMAN)` の場合:
  - 霊媒師騙りはしない。
  - 占い師騙りか潜伏を中心に比較する。
  - 霊媒師騙りは 1 巡後の `2-0` / `2-1` 盤面を読んだ 2 巡目の条件付き選択肢だと伝える。
- `day_number == 1 and discussion_round == 2 and role in (Role.WEREWOLF, Role.MADMAN)` の場合:
  - 公開ログから `2-0` か `2-1` を読む。
  - `2-0` で自分がグレーなら、投票候補を狭める自然な霊媒騙りを検討する。
  - `2-1` なら、対抗霊媒として出ざるを得ない自然な霊媒騙りを検討してよい。
  - day 1 なので霊媒結果は出さない。

真霊媒師向け:
- `day_number == 1 and discussion_round == 1 and role is Role.MEDIUM` の場合:
  - 1 巡目では霊媒 CO せず、占い師 CO 数とグレーの狭まり方を見る。
- `day_number == 1 and discussion_round == 2 and role is Role.MEDIUM` の場合:
  - `2-0` が確定し自分がグレーなら霊媒 CO する。
  - 霊媒騙りが出た場合は当然対抗 CO する。
  - day 1 なので霊媒結果はない。

非対象 role:
- `Role.SEER` / `Role.KNIGHT` / `Role.VILLAGER` / `role=None` には、人狼・狂人向けの霊媒騙り実行指示を出さない。
- 既存の 80〜300 字、`intent=speak` / `intent=skip`、2 人狼セット推理の基本 task text は維持する。
- day 2 以降 1 巡目の能力結果提示 rule は維持する。

## 5. 既存方針との整合を維持する

維持する既存方針:
- 単独 CO は、公開ログ上一度も対抗 CO がなければ真寄りに扱う。
- 過去に対抗 CO 履歴がある場合、現在 1 人だけ残った CO 者を自動真置きしない。
- 3-1、2-2、2-1、1-2 の基本進行を維持する。
- CO 超過分合計 3 による非 CO 確白級推理を維持する。
- 3 占い CO 盤面で、人狼・狂人は追加の霊媒師騙りや騎士騙りをしない。
- 霊媒白は「本物の人狼ではない」だけを示し、狂人や真役職の可能性を消さない。
- 村人は村人 CO / 素村 CO をしない。
- 人狼・狂人の偽結果は、この bot の実ルール、公開ログ、処刑・襲撃履歴、過去の自分の結果、対抗結果と矛盾させない。
- 騎士の護衛履歴は合法でなければならない。

やってはいけないこと:
- 配役を変える。
- ルールエンジンを変える。
- 状態遷移を変える。
- DB schema を増やす。
- slash command を増やす。
- LLMAction / structured output schema を変える。
- Discord API から message history を直接 prompt に流す。
- CO parser や自動盤面分類を追加する。
- 人狼・狂人の霊媒 CO をコードで強制する。
- 非人狼に人狼・狂人向けの霊媒騙り実行指示を見せる。
- 狂人に本物の狼位置が見えている前提で書く。
- 人狼・狂人の騙り全般を禁止する。
- day 1 2 巡目の条件付き霊媒騙りまで禁止する。
- 真霊媒師に存在しない day 1 霊媒結果を出させる。

## 6. 必要なテスト変更

`tests/test_llm_prompt_builder.py` を更新する:
- `_build_strategy_block(Role.WEREWOLF)` に、day 1 の 1 巡目では霊媒師騙りをしない趣旨が含まれること。
- `_build_strategy_block(Role.WEREWOLF)` に、day 1 の 2 巡目、`2-0`、自分がグレー位置、投票候補を狭める自然な霊媒 CO、`2-1`、対抗霊媒、出ざるを得ない形の趣旨が含まれること。
- `_build_strategy_block(Role.MADMAN)` に、day 1 の 1 巡目では霊媒師騙りをしない趣旨が含まれること。
- `_build_strategy_block(Role.MADMAN)` に、day 1 の 2 巡目、`2-0`、自分がグレー位置、投票候補を狭める自然な霊媒 CO、`2-1`、対抗霊媒、出ざるを得ない形の趣旨が含まれること。
- `_build_strategy_block(Role.MADMAN)` に、狂人は本物の狼位置を知らないため、グレーを狭めて人狼を処刑候補に近づける危険がある趣旨が含まれること。
- `_build_strategy_block(Role.MEDIUM)` に、day 1 の 1 巡目では霊媒 CO しない趣旨が含まれること。
- `_build_strategy_block(Role.MEDIUM)` に、day 1 の 2 巡目、`2-0`、自分がグレー位置、投票候補を狭めるために CO する趣旨が含まれること。
- `_build_strategy_block(Role.MEDIUM)` に、霊媒騙りが出た場合は当然対抗 CO し、まだ処刑がないため霊媒結果はない趣旨が含まれること。
- `task_daytime_speech(1, discussion_round=1, role=Role.WEREWOLF)` と `role=Role.MADMAN` には、1 巡目では霊媒師騙りをしない趣旨が含まれること。
- 上記 day 1 round 1 task から、旧文面の `占い師騙り・霊媒師騙り・潜伏の 3 択` が消えていること。
- `task_daytime_speech(1, discussion_round=2, role=Role.WEREWOLF)` と `role=Role.MADMAN` には、`2-0` / `2-1` の条件付き霊媒騙り guidance が含まれること。
- `task_daytime_speech(1, discussion_round=1, role=Role.MEDIUM)` には、1 巡目では霊媒 CO しない趣旨が含まれること。
- `task_daytime_speech(1, discussion_round=2, role=Role.MEDIUM)` には、`2-0` グレー CO と霊媒騙りへの対抗 CO の趣旨が含まれること。
- 非対象 role (`Role.SEER`, `Role.KNIGHT`, `Role.VILLAGER`, `role=None`) の task text に、人狼・狂人向けの霊媒騙り実行指示が含まれないこと。
- 狂人 strategy に `相方` や `襲撃先を揃える` が漏れないことを維持すること。
- 村人・占い師・騎士 strategy に霊媒騙り実行指示が漏れないこと。

`tests/test_llm_service.py` を更新する:
- 人狼 role の `system_prompt` に、day 1 1 巡目の霊媒騙り抑止と、day 1 2 巡目の `2-0` / `2-1` 条件付き霊媒騙り方針が届くこと。
- 狂人 role の `system_prompt` に、day 1 1 巡目の霊媒騙り抑止と、day 1 2 巡目の `2-0` / `2-1` 条件付き霊媒騙り方針が届くこと。
- 霊媒師 role の `system_prompt` に、day 1 1 巡目潜伏、day 1 2 巡目 `2-0` グレー CO、霊媒騙りへの対抗 CO 方針が届くこと。
- `_do_one_discussion_speech(discussion_round=1)` の captured task block で、人狼・狂人に旧 `3 択` 文面が届かないこと。
- `_do_one_discussion_speech(discussion_round=2)` の captured task block で、人狼・狂人に `2-0` / `2-1` 条件付き霊媒騙り guidance が届くこと。
- `_do_one_discussion_speech(discussion_round=1/2)` の captured task block で、真霊媒師に新しい 1 巡目潜伏 / 2 巡目 CO guidance が届くこと。
- 村人・占い師・騎士 role の `system_prompt` に、人狼・狂人向けの霊媒騙り実行指示が漏れないこと。
- 狂人 role の `system_prompt` に bare `相方` や `襲撃先を揃える` が含まれないことを維持すること。

既存テスト群は壊さないこと:
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_trigger.py`
- `tests/test_rules_misc.py`
- `tests/test_rules_night_targets.py`

受け入れ条件:
- 人狼 LLM は day 1 の 1 巡目に霊媒師騙りをしない。
- 狂人 LLM は day 1 の 1 巡目に霊媒師騙りをしない。
- 人狼・狂人 LLM は、day 1 の 2 巡目で `2-0` 盤面が確定し、自分がグレー位置なら、投票候補を狭める自然な霊媒 CO として霊媒騙りを検討できる。
- 人狼・狂人 LLM は、day 1 の 2 巡目で `2-1` 盤面が確定した場合、対抗霊媒として出ざるを得ない自然な霊媒 CO を検討できる。
- 真霊媒師 LLM は day 1 の 1 巡目に CO しない。
- 真霊媒師 LLM は day 1 の 2 巡目で `2-0` 盤面が確定し、自分がグレー位置なら霊媒 CO する。
- 真霊媒師 LLM は day 1 の 2 巡目で霊媒騙りが出た場合、当然対抗 CO する。
- day 1 の霊媒 CO では、真でも偽でも存在しない霊媒結果を出さない。
- LLM user context、DB schema、状態遷移、Discord command、structured output schema は変更されていない。
- 既存の単独 CO、対抗 CO 履歴、3-1 / 2-2 / 2-1 / 1-2、霊媒白、CO 超過分合計 3、3 占い CO で追加霊媒・騎士騙りをしない方針、村人 CO 禁止、偽結果整合、騎士合法護衛履歴の方針が壊れていない。

実行する検証コマンド:
- `uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py tests/test_llm_structured_output.py tests/test_llm_trigger.py tests/test_rules_misc.py tests/test_rules_night_targets.py`
- `uv run ruff check src tests`
- `uv run mypy`
```
