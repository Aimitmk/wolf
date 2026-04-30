# `wolfbot` 2026-04-27 LLM 残り縄・残り人狼数の共通勝ち筋強化プロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player を強い熟練した人狼プレイヤーとして振る舞わせることです。特に、9 人村は「残り縄数のうち、推定残り人狼数ぶんを投票で吊り切る必要があるゲーム」であることを、LLM の共通ルールとして明文化します。LLM が縄計算を単なる用語として読むのではなく、日々の投票・決め打ち・ローラー継続判断に使えるようにします。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player を、より強い熟練した 9 人村プレイヤーとして振る舞わせる。
- LLM player の共通ルールに、「残り縄の中で、推定残り人狼数ぶんを投票で吊り切る必要があるゲーム」という勝ち筋意識を明文化する。
- `縄計算` を単なる残り処刑回数の説明に留めず、`吊り余裕 = 残り縄 - 推定残り人狼数` として投票判断・ローラー判断・決め打ち判断へ使わせる。
- 変更は LLM prompt / prompt tests 周辺に閉じる。ゲームルール、勝利条件、DB schema、Discord command、状態遷移、LLM structured output schema は変えない。

今回必ず対応すること:
1. `_build_game_rules_block()` の共通ルールに、9 人村では残り縄数のうち推定残り人狼数ぶんを人狼処刑に使う必要があることを明記する。
2. `残り縄 - 推定残り人狼数` が実質的な吊り余裕であり、0 以下なら以後の投票ミスが敗着になり得ることを明記する。
3. `残り縄 == 推定残り人狼数` の局面では、非狼濃厚位置・狂人候補・真寄り情報役・確白級を安易に吊らず、人狼候補へ投票を集中させる必要があることを明記する。
4. `残り縄 > 推定残り人狼数` の局面でも、余裕分をどう使うかを CO 履歴・判定履歴・投票履歴・噛み筋・PP/RPP リスクから説明させる。
5. LLM の投票理由では、「この投票が残り縄の中で人狼を吊る計画にどうつながるか」を短く意識させる。
6. 残り人狼数は、bot が秘匿情報として教えるものではなく、公開ログ・霊媒結果・CO 破綻・襲撃死・投票履歴から LLM が推定する値として扱う。
7. 既存の `縄計算`、`PP/RPP`、`グレラン`、`ローラー`、`決め打ち`、`確白/確黒`、`3-1 / 2-2`、`2 人狼仮説` の方針と矛盾させない。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築とテストを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つこと。今回の変更でルールエンジン、勝利条件、投票解決、状態遷移を変えない。
- LLM prompt 構築の変更は原則として `src/wolfbot/llm/prompt_builder.py` と関連 tests に閉じること。
- `src/wolfbot/prompts/llm_system_prompt.md` の大構造は変えない。既存の `{game_rules_block}` と task block の差し込み構造を使うこと。
- user context に実際の残り人狼数、役職推定表、CO 自動集計、盤面分類器を追加しないこと。残り人狼数は公開情報から推定する前提を保つ。
- Discord channel history を直接 prompt に入れてはいけない。LLM 文脈は既存どおり DB の public/private logs からだけ構築すること。
- 非狼 prompt に狼相方情報や狼専用 strategy を混ぜないこと。
- 狂人 prompt に本物の人狼位置を知っている前提を書かないこと。
- 無関係な refactor をしないこと。
- 作業ツリーに既存の未コミット変更や未追跡ファイルがあっても、今回の目的に無関係なら戻さないこと。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/domain/rules.py`
- `src/wolfbot/domain/state_machine.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_rules_votes.py`
- `tests/test_state_machine_votes.py`

このリポジトリで確認済みの事実:
- ゲームは 9 人村固定で、配役は `人狼2 / 狂人1 / 占い師1 / 霊媒師1 / 騎士1 / 村人3`。
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md`。
- system prompt は `src/wolfbot/llm/prompt_builder.py::build_system_prompt()` が seat ごとに合成する。
- 共通ルールは `src/wolfbot/llm/prompt_builder.py::_build_game_rules_block()` にある。
- user context の縄数表示は `src/wolfbot/llm/prompt_builder.py::_format_rope_block()` で作られており、生存人数から残り処刑回数の目安を出している。
- 現在の共通ルールには、すでに `縄計算`、`floor((生存人数 - 1) / 2)`、9 人村開始時 `4縄`、`PP/RPP`、`2 人狼仮説` が含まれている。
- 現在の `_format_rope_block()` は「残り人狼数と狂人生存は公開情報から推定する必要があります」と案内している。
- `task_vote()` は LLM に合法候補 token から投票先を返させ、単体黒要素だけでなく 2 人狼セットとして票筋・噛み筋が自然かも考えさせている。
- `tests/test_llm_prompt_builder.py` は、共通ルール、縄計算、熟練用語、role-specific strategy の重要文言を文字列断片で固定している。
- `tests/test_llm_service.py` には、`LLMAdapter._ask()` が組み立てた `system_prompt` に共通ルールや role-specific strategy が届くことを確認するテストがある。

実装要求

## 1. 共通ルール block に「残り縄と残り人狼数」の勝ち筋を追加する

必要な仕様:
- LLM は、残り縄を「あと何回処刑できるか」だけでなく、「そのうち何本を人狼処刑に使わなければならないか」として扱う。
- 推定残り人狼数が 2 なら、村人陣営は原則として残り縄の中で 2 回、人狼を処刑する必要がある。
- 推定残り人狼数が 1 なら、残り縄の中で最後の 1 人狼を処刑する必要がある。
- `吊り余裕` は `残り縄 - 推定残り人狼数` として考える。
- `吊り余裕 > 0` なら、ローラー、情報吊り、灰吊りなどを検討できる余地がある。ただし狂人生存や PP/RPP が近いなら余裕を小さく見る。
- `吊り余裕 == 0` なら、以後の処刑はすべて人狼に当てる必要がある局面として扱う。非狼濃厚位置、真寄り情報役、確白級、単に狂人っぽいだけの位置を吊ると敗着になり得る。
- `吊り余裕 < 0` に見える場合は、推定が破綻しているか、すでに PP/RPP や敗勢に近い局面であるため、公開情報を再整理する。
- 残り人狼数は実役職から直接教えない。公開ログ上の霊媒結果、処刑結果、襲撃死、CO 破綻、投票、対抗 CO 数から推定する。
- 狂人は勝敗判定では人狼陣営だが、勝利条件の生存人数計算では非人狼として数える既存ルールを維持する。

実装方針:
- `src/wolfbot/llm/prompt_builder.py::_build_game_rules_block()` の既存 `縄計算` / `PP/RPP` / `発言の根拠チェックリスト` 付近へ短い bullet を追加する。
- 既存の共通ルールにある勝利条件、霊媒白の意味、3-1 / 2-2 進行、確白/確黒、CO 超過分推理を削らない。
- 長文の例を増やしすぎず、実戦判断に効く文面へ圧縮する。
- `_format_rope_block()` は必要なら短く補強してよい。ただし実際の残り人狼数を表示しない。公開情報から推定する前提を維持する。
- `build_system_prompt()` の公開引数、`LLMAction` schema、LLM provider 実装は変えない。

推奨文面例:
- `縄計算では、残り縄のうち推定残り人狼数ぶんを人狼処刑に使う必要がある。吊り余裕は「残り縄 - 推定残り人狼数」として考える。`
- `吊り余裕が 0 の局面では、以後の投票をすべて人狼に当てる必要がある。非狼濃厚位置・真寄り情報役・確白級・単に狂人っぽいだけの位置を吊ると敗着になり得る。`
- `吊り余裕がある局面でも、その余裕をローラー・情報吊り・灰吊りのどれに使うかは、CO 履歴、判定履歴、票筋、噛み筋、PP/RPP リスクで説明する。`
- `残り人狼数は公開情報からの推定であり、実役職を知らない立場では断定しすぎない。霊媒結果・CO 破綻・襲撃死・投票履歴から推定根拠を示す。`

## 2. 投票 task の判断軸を必要最小限補強する

必要な仕様:
- 投票時の LLM は、単に「最も怪しい人」ではなく、「残り縄の中で人狼を吊り切る計画上、今日処刑すべき人」を選ぶ。
- `reason_summary` には、公開発言のような長文ではなく、残り縄・推定残り人狼数・吊り余裕に関係する最も効く理由を短く残す。
- 決選投票でも、吊り余裕がない局面なら非狼濃厚位置への票逸らしを避け、人狼候補として最も筋が通る候補へ投票する。
- 人狼専用の相方情報・身内票・ライン切り guidance は既存どおり人狼本人だけに渡す。

実装方針:
- `task_vote()` の base text に、必要なら 1〜2 文だけ追加する。
- 候補順 pseudo-shuffle、target resolver、投票集計、決選処理は変更しない。
- 人狼専用 block に相方情報がある既存構造は維持し、非狼 role に漏らさない。
- 共通投票 text では `相方候補` のような公開ログ推理用語は使ってよいが、実際の相方を知っている前提にはしない。

推奨文面例:
- `今日の投票は、残り縄の中で推定残り人狼数を吊り切る計画に照らして選ぶ。吊り余裕が少ないほど、非狼濃厚位置や単に狂人っぽい位置への投票を避ける。`
- `reason_summary では、CO 履歴・判定・票筋・噛み筋に加えて、この投票が縄計算上なぜ今日必要かを短く示す。`

## 3. 情報秘匿と既存方針を維持する

必ず守ること:
- 非人狼の prompt に実際の狼位置や狼相方情報を渡さない。
- 狂人は本物の人狼位置を知らない前提を維持する。
- 人狼本人だけが、自分の相方情報を私的情報として扱う。
- 共通ルールの `推定残り人狼数` は、公開情報からの推理値として書く。
- `残り縄` の自動計算は生存人数からの目安に留める。残り人狼数の自動推定や DB schema 追加はしない。
- `PP/RPP` の説明は、狂人が勝敗判定上は人狼陣営だが、生存人数計算では非人狼である既存ルールと整合させる。

やってはいけないこと:
- 配役を変える
- 勝利条件を変える
- ルールエンジンを変える
- 投票解決や決選処理を変える
- DB schema を増やす
- slash command を増やす
- LLMAction / structured output schema を変える
- Discord API から message history を直接 prompt に流す
- CO parser、自動盤面分類器、残り人狼数推定器を追加する
- 非狼に狼相方情報を見せる
- 狂人に本物の狼位置が見えている前提で書く
- 無関係な refactor を広げる

必要なテスト変更

## `tests/test_llm_prompt_builder.py` を更新する
- `_build_game_rules_block()` に、残り縄のうち推定残り人狼数ぶんを人狼処刑に使う必要がある趣旨が含まれること。
- `_build_game_rules_block()` に、`吊り余裕` が `残り縄 - 推定残り人狼数` である趣旨が含まれること。
- `_build_game_rules_block()` に、吊り余裕 0 では以後の投票を人狼に当てる必要がある趣旨が含まれること。
- `_build_game_rules_block()` に、非狼濃厚位置・真寄り情報役・確白級・単に狂人っぽい位置を吊るリスクが含まれること。
- `_build_game_rules_block()` に、残り人狼数は公開情報から推定する趣旨が含まれること。
- `task_vote()` を補強した場合は、投票 task に残り縄・推定残り人狼数・吊り余裕を投票判断へ使う趣旨が含まれること。
- 既存の `縄計算`、`PP/RPP`、`グレラン`、`確白/確黒`、`3-1 / 2-2`、role-specific strategy leak 防止テストは壊さないこと。

## `tests/test_llm_service.py` を更新する
- `_CapturingDecider` など既存の捕捉用 decider を使い、`LLMAdapter._ask()` が組み立てた `system_prompt` に、残り縄と推定残り人狼数の共通勝ち筋ルールが届くことを検証する。
- 代表 role として `Role.VILLAGER`, `Role.SEER`, `Role.WEREWOLF`, `Role.MADMAN` など複数 role に共通ルールが届くことを確認する。
- 非狼 prompt に狼相方情報や狼専用 strategy が入らない既存保証を維持すること。
- 狂人 prompt に本物の人狼位置を知っている前提がないことを維持すること。

既存テスト群は壊さないこと:
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_trigger.py`
- `tests/test_llm_resolver.py`
- `tests/test_rules_votes.py`
- `tests/test_state_machine_votes.py`

受け入れ条件:
- すべての LLM player が、system prompt で「残り縄の中で推定残り人狼数ぶんを投票で吊り切る必要がある」方針を受け取る。
- LLM player が、吊り余裕を `残り縄 - 推定残り人狼数` として扱える。
- LLM player が、吊り余裕 0 の局面で非狼濃厚位置や単に狂人っぽい位置を安易に吊らないよう促される。
- LLM player が、吊り余裕のある局面でも、その余裕をローラー・情報吊り・灰吊りのどれに使うかを公開情報で説明するよう促される。
- 残り人狼数は秘匿情報として渡されず、公開情報からの推定として扱われる。
- 既存の縄計算、PP/RPP、CO 評価、3-1 / 2-2、確白/確黒、投票 task、情報秘匿、role-specific strategy 分離が壊れていない。
- user context、DB schema、状態遷移、Discord command、structured output schema は変更されていない。

実行する検証コマンド:
- `uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py tests/test_llm_structured_output.py tests/test_llm_trigger.py tests/test_llm_resolver.py tests/test_rules_votes.py tests/test_state_machine_votes.py`
- `uv run ruff check src tests`
- `uv run mypy`

最後に簡潔に報告すること:
- 何を変えたか
- どのファイルを変えたか
- 追加したテスト
- 実行した検証コマンドと結果
- 残課題があればその内容
```
