# `wolfbot` 2026-04-27 LLM 2-2 / 1-2 盤面誘発・霊媒騙り強化プロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player を強い熟練した人狼プレイヤーとして振る舞わせることです。現状は `3-1` や `2-1` 盤面は起こる一方、`2-2` や `1-2` 盤面がほぼ起きていないため、人狼・狂人が霊媒師騙りを戦略的に選べるように prompt / strategy を更新します。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player を、より強い熟練した人狼プレイヤーとして振る舞わせる。
- LLM の人狼・狂人が、占い師騙りだけに偏らず、霊媒師騙りを盤面形成の有力な選択肢として検討できるようにする。
- 現在よく起きる `3-1` / `2-1` だけでなく、`2-2` / `1-2` 盤面も自然に発生し得るようにする。
- ただし特定盤面をコードで強制生成せず、公開ログ・CO 数・縄数・相方位置・誤爆リスクを読んだうえで熟練者らしく選ばせる。

今回必ず対応すること:
1. 人狼・狂人の role-specific strategy に、day 1 から霊媒師騙りを現実的な選択肢として明記すること。
2. 人狼・狂人に、`2-2` と `1-2` が人外側にとって作る価値のある盤面であることを教えること。
3. 霊媒師騙りを「対抗占い師が既に出た day 2 以降だけの後追い選択肢」に限定しないこと。
4. 霊媒師騙りを選ぶ場合、day 1 は処刑がまだないため霊媒結果を出さず、day 2 以降の 1 巡目では前日処刑者への霊媒結果を必ず出すこと。
5. 狂人は本物の人狼位置を知らない前提を維持し、誤爆・誤支援リスクを踏まえて霊媒結果を選ぶこと。
6. 人狼は相方位置・襲撃方針・占い騙りとの役割分担を踏まえて、占い師騙り、霊媒師騙り、潜伏を比較すること。
7. 既存の情報秘匿、role-specific strategy 分離、persona 話法、structured output、候補トークン完全一致を壊さないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築、役職別 strategy、LLM discussion / vote / night action flow、関連テストを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つこと。今回の変更で配役、ルールエンジン、勝敗判定、状態遷移、投票解決、夜処理を変えない。
- DB schema は変更しない。
- slash command や Discord UI は追加しない。
- `LLMAction` schema は変更しない。
- Discord channel history を直接 prompt に入れない。LLM 文脈は既存どおり DB の public/private logs から構築すること。
- user context に新しい CO parser、盤面分類器、役職推定メモを追加しない。LLM 自身が公開ログから CO を読む設計を維持する。
- 特定の LLM に機械的に霊媒師騙りを強制しない。強制ではなく、熟練者が選択肢として自然に採る確率を上げる prompt 強化にする。
- 実装後は必ず関連 pytest、ruff、mypy を走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/domain/enums.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_trigger.py`

このリポジトリで確認済みの事実:
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md`。
- system prompt は `src/wolfbot/llm/prompt_builder.py::build_system_prompt()` が seat ごとに合成する。
- 共通ルールは `src/wolfbot/llm/prompt_builder.py::_build_game_rules_block()` にある。
- 役職別の立ち回りは `_ROLE_STRATEGIES[role]` にあり、人狼向けは `_ROLE_STRATEGIES[Role.WEREWOLF]`、狂人向けは `_ROLE_STRATEGIES[Role.MADMAN]` にある。
- `_build_game_rules_block()` には既に `3-1`、`2-2`、`2-1`、`1-2` の盤面定義と基本進行がある。
- 現在の人狼・狂人 strategy には、day 1 占い師騙り、day 2 以降の霊媒師騙り / 騎士騙り、偽結果の整合性、騙りすぎ注意が既にある。
- ただし現状の霊媒師騙りは「既に対抗占い師 CO が出ている場合は day 2 以降に検討」という後追い寄りの文面で、day 1 から `2-2` / `1-2` を作るための盤面形成としては弱い。
- `task_daytime_speech(day_number, discussion_round)` は現在 role-agnostic で、day 2 以降 1 巡目に CO 済み / CO する役職へ能力結果の提示を促している。
- `LLMAdapter` は LLM discussion を fire-and-forget で投げる設計なので、通常 advance path で LLM 応答を長時間 await する設計に変えてはいけない。

実装方針:
- 変更範囲は原則 `src/wolfbot/llm/prompt_builder.py` と関連 tests に閉じる。
- 既存の `_build_game_rules_block()` にある `2-2` / `1-2` 盤面説明は維持し、必要なら「人外側が意図的に作り得る盤面」として短く補強する。
- 主な変更は `_ROLE_STRATEGIES[Role.WEREWOLF]` と `_ROLE_STRATEGIES[Role.MADMAN]` に入れる。
- 必要なら `task_daytime_speech()` を後方互換の optional 引数で拡張し、人狼・狂人の昼議論だけに短い騙り選択 guidance を追加する。
- 既存呼び出しを壊さないため、optional 引数を追加する場合は default を現行挙動にする。

## 1. 人狼 strategy に day 1 霊媒師騙りと盤面形成を追加する

`_ROLE_STRATEGIES[Role.WEREWOLF]` を更新し、以下の趣旨を必ず入れること。

必須内容:
- day 1 の騙り選択は、占い師騙りだけでなく、霊媒師騙り、潜伏を比較する。
- `2-2` は、占い師 2 CO + 霊媒師 2 CO にして村の情報役を両方とも確定させず、霊媒ローラーや決め打ち負担を発生させる盤面である。
- `1-2` は、占い師を 1 CO に残しつつ霊媒師 2 CO にして、霊媒ローラーで縄を使わせたり、霊媒結果を割って議論を歪めたりできる盤面である。
- 相方または狂人らしい人物が占い師騙りに出ていそうな場合、自分が霊媒師騙りに回ると `2-2` を作れる可能性がある。
- 占い師 CO が 1 人だけで止まっている場合、自分が霊媒師騙りをすることで `1-2` を作り、単独霊媒を確定させない選択肢がある。
- day 1 の霊媒師騙りでは、まだ処刑が発生していないため霊媒結果は出さない。「霊媒師として出る」「明日から処刑結果を見る」のように CO する。
- day 2 以降に霊媒師騙りを継続する場合は、昼の議論 1 巡目で前日処刑者への霊媒結果を必ず出す。
- 人狼が霊媒師騙りに出る場合は、ローラーで自分が処刑されるリスク、相方の生存価値、残り縄、占い騙りとの役割分担、襲撃方針を比較する。
- 人狼本体を失うリスクがあるため、常に霊媒師騙りを選ぶ固定ルールにはしない。

推奨文面例:

```text
- day 1 の騙り選択は、占い師騙りだけでなく霊媒師騙りも現実的な候補に入れる。占い師 2 CO 付近で自分が霊媒師に出ると 2-2 を作り、占い・霊媒の両方を確定させず、霊媒ローラーや決め打ち負担を村に押し付けられる。
- 占い師 CO が 1 人だけで止まっているとき、自分が霊媒師騙りに出ると 1-2 を作れる場合がある。単独霊媒を確定させず、霊媒ローラーで縄を使わせる価値がある。
- day 1 の霊媒師騙りでは、まだ処刑結果がないため霊媒結果を出さない。day 2 以降は前日処刑者への霊媒結果を 1 巡目で必ず出す。
- 霊媒師騙りはローラーで人狼本体を失う危険がある。相方の位置、占い騙りとの役割分担、残り縄、PP/RPP への近さを見て、占い騙り・霊媒騙り・潜伏を選び分ける。
```

注意:
- 人狼 strategy には `相方`、`襲撃`、役割分担などの人狼専用判断を書いてよい。
- ただしこの文面を共通ルールや非人狼 strategy に漏らさないこと。

## 2. 狂人 strategy に day 1 霊媒師騙りと盤面形成を追加する

`_ROLE_STRATEGIES[Role.MADMAN]` を更新し、以下の趣旨を必ず入れること。

必須内容:
- 狂人は本物の人狼位置を知らないが、人狼陣営の勝利のために村の情報整理を乱す。
- day 1 の基本候補は占い師騙りだけでなく、霊媒師騙りも含める。
- `2-2` は、占い師 2 CO + 霊媒師 2 CO にして、真占い・真霊媒の両方を確定させない有効な盤面である。
- `1-2` は、霊媒師を複数 CO にして霊媒ローラーや霊媒結果の混乱を起こせる盤面である。
- 既に占い師 CO が 2 人前後いる、または占い師騙りが増えすぎそうな場合、狂人は霊媒師騙りに回る価値がある。
- 占い師 CO が 1 人だけで止まっており、霊媒師 CO が 1 人出ている場合、狂人が霊媒師対抗 CO して `1-2` を作る価値がある。
- day 1 の霊媒師騙りでは、まだ処刑結果がないため霊媒結果を出さない。
- day 2 以降は、前日処刑者への霊媒結果を必ず出す。ただし狂人は本物の人狼位置を知らないため、霊媒黒・霊媒白のどちらも誤支援 / 誤爆リスクがある。
- 霊媒師騙りは自分がローラーされやすいが、狂人が縄を消費させること自体は人狼陣営に有利な場合がある。
- 常に霊媒師騙りを選ぶ固定ルールにはせず、CO 数、縄数、占い師 CO 数、霊媒師 CO 数、公開ログ上の信用差で選び分ける。

推奨文面例:

```text
- day 1 は占い師騙りだけでなく、霊媒師騙りも強い基本候補に入れる。占い師 CO が 2 人前後で自分が霊媒師に出ると 2-2 を作り、真占い・真霊媒の両方を確定させにくくできる。
- 占い師 CO が 1 人、霊媒師 CO が 1 人で止まりそうなときは、霊媒師対抗 CO で 1-2 を作り、霊媒ローラーや霊媒結果の混乱に持ち込む価値がある。
- day 1 の霊媒師騙りでは処刑結果がないため、結果は出さず霊媒師として名乗るだけにする。day 2 以降は前日処刑者への霊媒結果を 1 巡目で必ず出す。
- 狂人は本物の人狼位置を知らない。霊媒黒で本物の人狼を切ってしまうリスク、霊媒白で真占いを補強してしまうリスクを見て、公開ログと縄数に合う結果を選ぶ。
```

注意:
- 狂人 strategy に `相方`、`襲撃先を揃える`、本物の人狼位置を知っている前提の文面を入れないこと。
- `相方候補` は公開ログからの推理語彙としてなら使ってよいが、実際の人狼を知っているように書かないこと。

## 3. 共通ルールの 2-2 / 1-2 説明を壊さず、必要なら短く補強する

既存の `_build_game_rules_block()` には `2-2` / `1-2` の盤面説明がある。これを削除・弱体化しないこと。

必要なら、以下の趣旨を共通ルールに短く追加してよい。

追加してよい内容:
- `2-2` / `1-2` は異常盤面ではなく、9 人村で人狼・狂人の騙りによって自然に起こり得る盤面である。
- 村陣営は `2-2` / `1-2` で霊媒師を自動的に真置きしない。
- 霊媒結果が割れた場合、どちらか片方だけを根拠なく信じず、占い結果・投票・襲撃・CO 時系列と合わせて見る。

注意:
- 共通ルールには「人狼・狂人は 2-2 を作れ」のような人外専用実行指示を書かない。
- 共通ルールは全役職に届くため、狼専用語彙 `相方` や `襲撃先を揃える` を入れない。

## 4. 必要なら `task_daytime_speech()` を role-aware にする

prompt 文面だけで day 1 の行動が変わりにくい場合は、`src/wolfbot/llm/prompt_builder.py::task_daytime_speech()` を後方互換に拡張してよい。

推奨シグネチャ:

```python
def task_daytime_speech(
    day_number: int,
    discussion_round: int | None = None,
    *,
    role: Role | None = None,
) -> str:
    ...
```

必須内容:
- 既存の `task_daytime_speech(day_number, discussion_round)` 呼び出しはそのまま動くこと。
- `role is Role.WEREWOLF` または `role is Role.MADMAN` の場合だけ、昼議論 task に短い騙り選択 guidance を追加してよい。
- 非人狼 role、`role=None` では現行の一般昼議論 task に留める。
- day 1 では「占い師騙りだけでなく霊媒師騙りも盤面次第で検討する」「霊媒師騙りの場合は処刑結果がないので結果を出さない」を伝える。
- day 2 以降 1 巡目では、既存の能力結果提示 rule を維持し、霊媒師騙り中なら前日処刑者への霊媒結果を出すことを伝える。

推奨文面例:

```text
 あなたが人狼陣営として偽 CO を検討する場合、占い師騙りだけでなく霊媒師騙りも候補に入れてください。2-2 や 1-2 を作ると、霊媒を確定させずローラーや決め打ち負担を村に押し付けられます。day 1 の霊媒師騙りでは処刑結果がないため、結果は出さず CO だけにします。
```

`LLMAdapter` 側で `task_daytime_speech()` を呼ぶ箇所がある場合:
- actor の `Player.role` を渡せる箇所だけ `role=player.role` を渡す。
- role が取れない既存呼び出しは無理に変えない。
- 非人狼に人外向け guidance を渡さない。
- LLM discussion の fire-and-forget / stale guard / speech count tracking を変えない。

## 5. テストを追加 / 更新する

`tests/test_llm_prompt_builder.py`:
- `_build_strategy_block(Role.WEREWOLF)` に `2-2`、`1-2`、`霊媒師騙り`、`霊媒ローラー`、`day 1`、`処刑結果がない`、`day 2 以降` が含まれること。
- `_build_strategy_block(Role.WEREWOLF)` に、占い師騙り・霊媒師騙り・潜伏を比較する趣旨が含まれること。
- `_build_strategy_block(Role.WEREWOLF)` に、霊媒師騙りはローラーで人狼本体を失うリスクがあるため固定行動ではない趣旨が含まれること。
- `_build_strategy_block(Role.MADMAN)` に `2-2`、`1-2`、`霊媒師騙り`、`霊媒ローラー`、`day 1`、`処刑結果がない`、`day 2 以降` が含まれること。
- `_build_strategy_block(Role.MADMAN)` に、狂人は本物の人狼位置を知らないため霊媒結果にも誤爆・誤支援リスクがある趣旨が含まれること。
- `_build_strategy_block(Role.MADMAN)` に `相方`、`襲撃先を揃える` が漏れないことを維持すること。
- 非人狼 role strategy に「2-2 を作る」「1-2 を作る」「霊媒師騙りを選ぶ」のような人外向け実行指示が漏れないこと。
- `task_daytime_speech()` を role-aware にした場合、通常呼び出しでは人外向け騙り guidance が出ず、`role=Role.WEREWOLF` / `role=Role.MADMAN` の場合だけ出ること。

`tests/test_llm_service.py`:
- 人狼 seat の system prompt に、新しい 2-2 / 1-2 形成と霊媒師騙り guidance が含まれること。
- 狂人 seat の system prompt に、新しい 2-2 / 1-2 形成と霊媒師騙り guidance が含まれること。
- 狂人 seat の system prompt に bare `相方` や `襲撃先を揃える` が含まれないことを維持すること。
- 村人・占い師・霊媒師・騎士 seat の system prompt に、人外専用の「2-2 / 1-2 を作るために霊媒師騙りをする」実行指示が含まれないこと。

実行するチェック:

```bash
uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py
uv run ruff check src tests
uv run mypy
```

やってはいけないこと:
- 配役を変える。
- 9 人村固定を変える。
- ルールエンジンや状態遷移を変える。
- 投票解決や夜処理を変える。
- DB schema を変更する。
- slash command や Discord UI を増やす。
- LLM の行動をコード側で強制的に霊媒師騙りへ変える。
- CO parser / 盤面分類器 / 役職推定メモを新規実装して user context に入れる。
- 非人狼に人外専用の偽 CO 実行指示を見せる。
- 狂人に本物の人狼位置や相方情報が見えている前提で書く。
- 無関係な refactor を広げる。

完了条件:
- LLM の人狼・狂人 strategy 上、day 1 から霊媒師騙りが明確な選択肢になっている。
- `2-2` / `1-2` は、村側の読み方だけでなく、人外側が作る価値のある盤面として人狼・狂人 strategy に反映されている。
- day 1 霊媒師騙りで存在しない霊媒結果を出さず、day 2 以降は前日処刑者への結果を出す方針が明文化されている。
- 情報秘匿と role-specific strategy 分離がテストで守られている。
- 既存の `3-1` / `2-1` guidance、単独 CO 評価、村人 CO 禁止、騎士護衛、投票 discipline を壊していない。
- 関連 pytest、ruff、mypy が通っている。
```
