# `wolfbot` 2026-04-24 LLM Player 強化プロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player を、秘匿性を壊さずに強化する。
- 主眼は「役職や状態の不正共有を防いだまま、固定ルール・初期配役・役職別戦略を prompt に与えて判断品質を上げること」であり、無関係な仕様変更はしない。

今回必ず対応すること:
1. LLM player 同士が互いの役職や状態を不正に共有しないことを、コード読解と回帰テストで明確に固定すること。
2. 9 人村の固定ルール、勝利条件、初期役職構成を LLM system prompt に読み込ませること。
3. 役職に応じた戦略 / Tips を LLM system prompt に読み込ませること。
4. ただし役職別 Tips は「その LLM 自身の現在役職」に対応するものだけを読み込ませ、他役職の内情や専用戦略を混ぜないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築と情報スコープを把握してから修正すること。
- `domain/` は純粋ロジックのまま保ち、LLM prompt 構築は `llm/` と `services/llm_service.py` に閉じ込めること。
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
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/domain/enums.py`
- `src/wolfbot/domain/rules.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`

このリポジトリで確認済みの事実:
- `LLMAdapter._ask()` は seat ごと・呼び出しごとに fresh な `system_prompt` と `user_context` を組み立てており、会話スレッド共有型の実装ではない。
- `build_user_context()` は `public_logs` と「その seat だけに見える `private_logs`」から user context を組み立てる。
- `LLMAdapter._ask()` は `repo.load_public_logs(game.id, ...)` と `repo.load_private_logs_for_audience(game.id, audience_seat=player.seat_no, ...)` を使っており、DB 読み出しは `game_id` 単位にスコープされている。
- `tests/test_llm_service.py` には、別 `game_id` の public/private logs が混ざらないことを確認する既存回帰テストがすでに存在する。
- 現在の `build_system_prompt()` は主に人格・自役職・現在フェイズ・今回タスクだけを system prompt に入れており、固定ルール・初期配役・役職別戦略の情報は十分ではない。
- 現在の `build_user_context()` では、人狼相方情報は `me.role is Role.WEREWOLF` の場合にのみ user context へ追加される。
- したがって今回の懸念に対する主修正点は「LLM 同士がメモリ共有していることの是正」ではなく、「system prompt を強化しつつ既存の情報分離を壊さないこと」である。

実装要求

## 1. LLM prompt に共通ルールブロックを追加する

必要な仕様:
- すべての LLM player が、毎回の `system_prompt` でこの村の固定ルール要約を受け取ること。
- 固定ルールは「この bot で実際に採用している 9 人村仕様」に合わせること。
- 共通ルールは system prompt に入れ、user context には入れないこと。

このタスクで固定する実装方針:
- `src/wolfbot/prompts/llm_system_prompt.md` に新しい placeholder を追加すること。
  - 例: `{game_rules_block}`
  - 名前は多少変えてよいが責務は明確にすること。
- `src/wolfbot/llm/prompt_builder.py` で、system prompt 用の共通ルール文字列を組み立てる helper を追加すること。
- 可能なら `build_system_prompt()` の公開引数は増やさず、内部 helper 追加で完結させること。
- 役職分布の文面は `ROLE_DISTRIBUTION` と `ROLE_JA` から生成し、固定文字列の二重管理を減らすこと。

共通ルールブロックに必ず含める内容:
- この村は 9 人村固定であること。
- 初期配役は `人狼2 / 狂人1 / 占い師1 / 霊媒師1 / 騎士1 / 村人3` であること。
- 村人陣営勝利は「生存人狼数が 0」、人狼陣営勝利は「生存人狼数が生存非人狼人数以上」であること。
- 昼は公開ログと自分が知る私的情報だけを根拠に話すこと。
- 他プレイヤーの役職・夜行動・判定・相方情報など、公開されていない情報を事実として扱ってはいけないこと。
- 占い師と霊媒師は「本物の人狼だけ黒」であり、狂人は黒判定されないこと。
- `NIGHT_0` の占いランダム白は「本物の人狼ではない」相手であること。
- 人狼は夜に意見が割れると襲撃失敗になり得ること。
- 騎士は同じ相手を連続護衛できないこと。
- 投票 / 夜行動では、提示された合法候補トークンだけから選ぶこと。

## 2. 役職別 strategy / tips ブロックを追加する

必要な仕様:
- すべての LLM player が、自分の現在役職に応じた戦略ブロックを system prompt で受け取ること。
- 役職別 Tips は role-specific であり、他役職の専用情報や内情を混ぜてはならない。
- 役職別 Tips は一般的な立ち回り指針に留め、ゲーム中に未公開の事実を補ってはならない。

このタスクで固定する実装方針:
- `src/wolfbot/prompts/llm_system_prompt.md` に role-specific strategy 用 placeholder を追加すること。
  - 例: `{strategy_block}`
- `src/wolfbot/llm/prompt_builder.py` に `Role -> strategy text` の pure helper を追加すること。
- strategy text は `Role` ごとに分岐し、現在 role のものだけを system prompt に差し込むこと。

各役職に必ず含めるべき戦略指針:

### 人狼
- 相方と襲撃先を揃えることを優先する。
- 昼の主張・投票理由・夜の襲撃意図に一貫性を持たせる。
- 相方を露骨に庇いすぎない。
- 情報役、信頼されている位置、盤面整理を進める相手を優先的に脅威として考える。

### 狂人
- 人狼勝利を助けるが、真の人狼位置を知っている前提で話してはならない。
- 偽 CO や偽結果を出す場合でも、公開情報との矛盾や破綻を避ける。
- 「知り得ない確定情報」を断言しない。

### 占い師
- 判定履歴を時系列で一貫して扱う。
- 黒結果は強い根拠として扱う。
- 白結果は「非人狼」であって完全村置きではないことを意識する。
- CO 状況、対抗の整合性、投票との噛み合いを見る。

### 霊媒師
- 処刑結果を占い主張や投票結果と照合する。
- 黒 / 白結果が占い視点に与える影響を整理する。
- 結果が出ていない段階では断定を増やしすぎない。

### 騎士
- 守る価値の高い情報役や信頼位置を意識する。
- 同一対象の連続護衛禁止を守る。
- 護衛先を不用意に公開して噛み筋ヒントを与えない。

### 村人
- 公開発言の矛盾、視点漏れ、投票理由、結果整合性を重視する。
- 不確実なときは候補を絞って理由つきで話す。
- 私的情報があるふりや、見えていない役職情報の断言をしない。

## 3. 情報秘匿を壊さないことをコードとテストで固定する

必要な仕様:
- 非人狼の prompt に狼相方情報が混ざってはいけない。
- 狼以外の role-specific strategy に、人狼専用の夜連携戦術が混ざってはいけない。
- 別 `game_id` の log は current game の prompt に混ざってはいけない。
- Discord の message history を直接拾って prompt に入れてはいけない。

このタスクで固定する作業:
- `LLMAdapter._ask()` の DB ベースの文脈構築は維持すること。
- `build_user_context()` の現在のスコープ分離は壊さないこと。
- system prompt の強化で role leak が起きないよう、テストで固定すること。
- 既存の `test_ask_scopes_logs_to_current_game_id` 相当の回帰保証は維持し、必要なら補強すること。

## 4. prompt 文面の品質ルール

必要な仕様:
- system prompt は日本語で統一すること。
- 「ルール要約」「役職別 Tips」「人格」「現在役職」「現在フェイズ」「今回タスク」の役割分離を明確にすること。
- ルールと戦略は強く書くが、未公開情報の捏造を促す文面にしないこと。
- 既存の JSON schema 出力制約、候補トークン一致制約、メタ発言禁止、日本語限定の制約は維持すること。

やってはいけないこと:
- 配役を変える
- ルールを bot 実装とズラす
- 狼以外に相方情報を見せる
- 狂人に本物の狼位置が見えている前提で書く
- Discord API から message history を直接 prompt に流す
- DB schema を増やす
- slash command を増やす
- 無関係な refactor を広げる

必要なテスト変更:

## `tests/test_llm_prompt_builder.py` を新規追加する
- system prompt に固定配役のルール要約が含まれること。
- `Role.SEER` の system prompt に seer 向け Tips が入り、werewolf 向け Tips は入らないこと。
- `Role.WEREWOLF` の system prompt に wolf 向け Tips が入り、villager 向け Tips は入らないこと。
- 既存の人格・役職・フェイズ・task block が壊れていないこと。

## `tests/test_llm_service.py` を更新する
- `_CapturingDecider` を使い、`_ask()` が組み立てた `system_prompt` に共通ルールブロックが入っていることを検証する。
- `_CapturingDecider` を使い、現在 role に応じた strategy block だけが system prompt に含まれることを検証する。
- 非狼の prompt に狼相方情報や狼専用 strategy が入らないことを検証する。
- 既存の「別 `game_id` の logs が混ざらない」回帰テストは維持すること。

## 既存テスト群は壊さないこと
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`

受け入れ条件:
- すべての LLM player が、system prompt でこの bot の固定ルール要約を受け取る。
- すべての LLM player が、自分の現在役職に対応する Tips だけを受け取る。
- 非狼に狼相方情報や狼専用連携戦略が漏れない。
- 別 `game_id` の logs が混ざらない既存保証を壊さない。
- user context の秘匿範囲を広げず、system prompt 強化だけで LLM の判断材料を増やせる。

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
