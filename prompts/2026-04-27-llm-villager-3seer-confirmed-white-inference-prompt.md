# `wolfbot` 2026-04-27 LLM 村陣営 3占いCO・2白確定推理アップデートプロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player、とくに村人陣営の LLM を、より強い熟練した人狼プレイヤーとして振る舞わせることです。9 人村固定配役で占い師 CO が 3 人出たあと、ゲーム進行に伴ってそのうち 2 人が本物の人狼ではないと公開情報上確定した場合、残り 1 人の占い師 CO は本物の人狼、つまり確定黒級の位置として推定・処理できるようにします。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player を、より強い熟練した人狼プレイヤーとして振る舞わせる。
- とくに村人陣営の LLM が、3 人の占い師 CO から 2 人の非狼が確定した盤面を正しく詰められるようにする。
- 9 人村固定配役 (`人狼2 / 狂人1 / 占い師1 / 霊媒師1 / 騎士1 / 村人3`) では、3 人の占い師 CO のうち 2 人が本物の人狼ではないと確定した場合、残る 1 人の占い師 CO は本物の人狼として扱える、と system prompt / role strategy に明文化する。
- 変更は LLM prompt / prompt test 周辺に閉じ、ゲームルール、DB schema、Discord command、状態遷移、LLM structured output schema は変えない。

今回必ず対応すること:
1. 3 人の占い師 CO が出ている盤面で、占い師 CO 者のうち 2 人が公開情報上「本物の人狼ではない」と確定した場合、残る占い師 CO 者を確定黒級の狼位置として推定する方針を追加すること。
2. 「白判定」と「非狼確定」を混同しないようにすること。信用未確定の占い師 CO による白判定、偽の可能性が残る霊媒結果、ただの印象白だけでは非狼確定として数えない。
3. 非狼確定の根拠は、公開ログ・霊媒結果・襲撃死・真寄り情報役の結果・CO 破綻整理など、bot のルールと公開情報の整合から説明できるものに限る。
4. 2 人の非狼が確定したなら、残る占い師 CO を「まだ灰の 1 人」や「単に怪しい候補」ではなく、固定配役上の狼位置として投票・発言・進行提案へ反映させる。
5. 例外として、人間プレイヤーが村陣営で占い師を騙ったことが公開情報上明確になった、CO 撤回が成立した、霊媒師偽が濃厚になったなど、前提の「2 人非狼確定」が崩れる場合は、確定黒扱いを解除して再整理する。
6. 既存の「村人 CO 禁止」「対抗 CO 履歴ありの残存 1 CO を自動真置きしない」「霊媒白は非狼だけを示す」「3-1 / 占いローラー / 黒ストップ」の方針を壊さないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築とテストを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つこと。今回の変更でルールエンジンや状態遷移を変えない。
- LLM prompt 構築は `src/wolfbot/llm/prompt_builder.py` と必要最小限の prompt tests 周辺に閉じること。
- `src/wolfbot/prompts/llm_system_prompt.md` の大構造は変えない。既存の `{game_rules_block}` と role-specific strategy の差し込み構造を使うこと。
- user context に新しい CO parser、盤面集計、役職推定表、DB schema を追加しないこと。今回は LLM が公開ログを読むときの推理方針を明文化する。
- Discord channel history を直接 prompt に入れてはいけない。LLM 文脈は既存どおり DB の public/private logs からだけ構築すること。
- 非狼 prompt に狼相方情報や狼専用 strategy を混ぜないこと。
- 人狼・狂人の偽 CO strategy は必要以上に弱体化しないこと。ただし村陣営が 3占いCO・2非狼確定を正しく詰める方針は、共通ルールとして読めるようにしてよい。
- 無関係な大規模 refactor をしないこと。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/domain/rules.py`
- `src/wolfbot/domain/state_machine.py`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/services/llm_service.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_rules_night_targets.py`
- `tests/test_rules_misc.py`

このリポジトリで確認済みの事実:
- ゲームは 9 人村固定で、配役は `人狼2 / 狂人1 / 占い師1 / 霊媒師1 / 騎士1 / 村人3`。
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md`。
- system prompt は `src/wolfbot/llm/prompt_builder.py::build_system_prompt()` が合成する。
- 共通ルールは `src/wolfbot/llm/prompt_builder.py::_build_game_rules_block()` にある。
- 役職別の立ち回りは `src/wolfbot/llm/prompt_builder.py::_ROLE_STRATEGIES` にある。
- 既存の共通ルールには、すでに `3-1`、占いローラー、黒ストップ、霊媒白の意味、対抗 CO 履歴ありの残存 1 CO を自動真置きしない方針が含まれている。
- 既存の `Role.VILLAGER` strategy には、村人 CO 禁止、公開情報からの推理、2 人狼仮説を作る方針が含まれている。
- `src/wolfbot/domain/rules.py::is_detected_as_wolf(role)` は `Role.WEREWOLF` だけを黒として扱い、狂人は白判定になる。
- `src/wolfbot/domain/rules.py::legal_attack_targets()` は襲撃対象から人狼を除外するため、通常の襲撃死は本物の人狼ではない公開情報として扱える。
- `tests/test_llm_prompt_builder.py` は、共通ルールと role-specific strategy の重要文言を文字列断片で固定している。
- `tests/test_llm_service.py` には、`LLMAdapter._ask()` が組み立てた `system_prompt` に共通ルールや role-specific strategy が届くことを確認するテストがある。

実装要求

## 1. 共通ルール block に 3占いCO・2非狼確定の詰め方を追加する

必要な仕様:
- 3 人の占い師 CO が出ている場合、固定配役上、占い師 CO の内訳は基本的に `真占い師 + 狂人または人狼の騙り + 人狼の騙り` として見る。
- 占い師 CO 者 3 人のうち 2 人が本物の人狼ではないと確定した場合、残る 1 人は本物の人狼として扱う。
- この推理は「白判定が 2 つ出たから」ではなく、「占い師 CO 者 2 人の非狼が公開情報上確定したから」成立する。
- 確定していない白を数えない。たとえば、信用未確定の対抗占い師の白、偽霊媒の可能性が残る霊媒白、発言印象だけの白、真偽不明の騎士主張は、単独では非狼確定として扱わない。
- 2 人非狼確定が成立した場合、残る占い師 CO は「確黒」「確定黒級」「本物の人狼として処刑優先」として扱い、投票・発言・進行提案へ反映する。
- ただし、村陣営騙りや CO 撤回などで「占い師 CO 3 人のうち非狼が 3 人あり得る」前提が公開情報上出た場合は、破綻や撤回の時系列を再整理する。

実装方針:
- `src/wolfbot/llm/prompt_builder.py` の `_build_game_rules_block()` に、既存の `3-1` / 占いローラー / 黒ストップ説明の近くへ文面を追加する。
- 既存の `3-1` 基本進行、黒ストップ、霊媒白の意味、確白・確黒の定義を削らない。
- 新しい文面は、すべての LLM seat が読める共通ルールとして入れる。これは盤面上の事実推理であり、役職固有の私的情報ではない。
- ただし、狼専用の夜連携語彙や相方情報を共通ルールへ混ぜない。
- parser や機械集計は追加しない。LLM が公開ログを読み、条件成立時に推理を強めるための prompt 文面に留める。

推奨文面例:
- `3 人の占い師 CO のうち 2 人が、霊媒結果・襲撃死・破綻整理など公開情報上「本物の人狼ではない」と確定した場合、固定配役上、残る占い師 CO は本物の人狼として扱う。`
- `これは単なる白判定 2 つでは成立しない。信用未確定の占い師 CO が出した白、偽の可能性が残る霊媒白、印象だけの白は非狼確定として数えない。`
- `2 人非狼確定が成立した 3占いCO盤面では、残る占い師 CO は確定黒級の処刑優先位置であり、「まだ灰の 1 人」として扱わない。`
- `村陣営の占い騙り・CO 撤回・霊媒偽などで前提が崩れる場合は、誰がいつ何を撤回し、どの情報が確定でなくなったかを時系列で再整理する。`

## 2. 村人陣営 role strategy を補強する

必要な仕様:
- 村人陣営の LLM は、3占いCO・2非狼確定の盤面を見たら、残る占い師 CO を強い狼位置として発言・投票に反映する。
- とくに `Role.VILLAGER` は、公開情報しかない立場として、CO 履歴、霊媒結果、襲撃死、投票、噛み筋から「どの 2 人が非狼確定で、なぜ残りが狼になるか」を短く説明できるようにする。
- `Role.SEER` は、対抗占い師 CO の中で 2 人非狼が確定した場合、自分視点の判定履歴と公開情報を合わせて、残る対抗を狼として詰める。
- `Role.MEDIUM` は、占い師 CO 処刑後の霊媒結果を使って、3占いCO内訳の非狼確定数を整理し、残る占い師 CO が狼になる条件を説明する。
- `Role.KNIGHT` は、守るべき情報役や処刑優先位置を判断するときに、3占いCO・2非狼確定から残る狼位置を考慮する。
- 人狼・狂人の role-specific strategy に、村人陣営専用の「投票で詰める」文面を混ぜない。

実装方針:
- 最低限 `Role.VILLAGER` strategy を補強する。
- 必要なら `Role.SEER` / `Role.MEDIUM` / `Role.KNIGHT` strategy に短い補足を追加する。
- 同じ長文を各 role に重複させすぎない。共通の推理ルールは `_build_game_rules_block()` に置き、role strategy は「自分の役職ならどう発言・投票へ使うか」に絞る。

推奨文面例:
- `3 人の占い師 CO のうち 2 人が非狼確定した場合、残る占い師 CO は固定配役上の狼位置として扱い、投票・処刑提案の優先度を上げる。`
- `村人は「誰が白っぽいか」ではなく、「どの占い師 CO 2 人が本物の人狼ではないと確定したか」を公開情報で示してから、残る占い師 CO を狼推定する。`
- `非狼確定ではない白判定や印象白を根拠に、残る占い師 CO を確定黒と言い切らない。`

## 3. 情報秘匿と既存方針を維持する

必要な仕様:
- 非人狼の prompt に狼相方情報が混ざってはいけない。
- 狂人には本物の人狼位置を知らせない。
- 村人・占い師・霊媒師・騎士は、自分に見えていない役職や夜行動を事実として断言しない。
- ただし公開情報から論理的に確定した非狼・狼推定は、推理として強く扱ってよい。
- 「白」は常に非狼を意味するだけで、狂人の可能性を消さない既存方針は維持する。
- `確黒` は単独偽占い候補から黒を出されただけでは成立しない既存方針を維持する。今回の確黒級推理は、3占いCOのうち 2 人非狼確定という配役上の消去法が揃った場合だけ成立する。

やってはいけないこと:
- 配役を変える
- ルールエンジンを変える
- 状態遷移を変える
- DB schema を増やす
- slash command を増やす
- LLMAction / structured output schema を変える
- Discord API から message history を直接 prompt に流す
- CO parser や自動盤面分類を追加する
- 非狼に相方情報を見せる
- 狂人に本物の狼位置が見えている前提で書く
- 無関係な refactor を広げる

必要なテスト変更

## `tests/test_llm_prompt_builder.py` を更新する
- `_build_game_rules_block()` に、3 人の占い師 CO のうち 2 人が本物の人狼ではないと確定した場合、残る占い師 CO を本物の人狼として扱う趣旨が含まれること。
- `_build_game_rules_block()` に、単なる白判定や印象白では非狼確定として数えない趣旨が含まれること。
- `_build_game_rules_block()` に、残る占い師 CO を「まだ灰の 1 人」として扱わない趣旨が含まれること。
- `_build_game_rules_block()` に、村陣営騙り・CO 撤回・霊媒偽などで前提が崩れる場合は再整理する趣旨が含まれること。
- `_build_strategy_block(Role.VILLAGER)` に、3占いCO・2非狼確定から残る占い師 CO を狼位置として投票・処刑提案へ反映する趣旨が含まれること。
- 必要に応じて `_build_strategy_block(Role.SEER)` / `_build_strategy_block(Role.MEDIUM)` / `_build_strategy_block(Role.KNIGHT)` の補足文面も固定する。
- 既存の role-specific strategy leak 防止テストは壊さないこと。

## `tests/test_llm_service.py` を更新する
- `_CapturingDecider` など既存の捕捉用 decider を使い、`LLMAdapter._ask()` が組み立てた村人 role の `system_prompt` に 3占いCO・2非狼確定推理の共通ルールと村人 strategy が届くことを検証する。
- 村人以外の村陣営 role (`Role.SEER`, `Role.MEDIUM`, `Role.KNIGHT`) にも、少なくとも共通ルールが届くことを検証する。
- 人狼・狂人の system prompt に共通ルールが届くのは許容してよいが、村人 role-specific strategy の専用文面を狼・狂人 strategy に重複追加しないことを確認する。
- 非狼 prompt に狼相方情報や狼専用 strategy が入らない既存保証を維持すること。

既存テスト群は壊さないこと:
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_rules_misc.py`
- `tests/test_rules_night_targets.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_trigger.py`

受け入れ条件:
- LLM 村人陣営の system prompt に、3 人の占い師 CO のうち 2 人が非狼確定した場合、残る占い師 CO を本物の人狼として扱う方針が明示されている。
- LLM は、単なる白判定や印象白を非狼確定として誤カウントしない。
- LLM 村人は、公開情報から「どの 2 人が非狼確定か」を示したうえで、残る占い師 CO を確定黒級として投票・処刑提案へ反映できる。
- LLM 占い師・霊媒師・騎士も、共通ルールとしてこの消去法を理解できる。
- 既存の 3-1 進行、占いローラー、黒ストップ、霊媒白の意味、確白・確黒の注意、村人 CO 禁止、対抗 CO 履歴ありの残存 1 CO 注意が壊れていない。
- LLM user context、DB schema、状態遷移、Discord command、structured output schema は変更されていない。
- 情報秘匿と role-specific strategy 分離が維持されている。

実行する検証コマンド:
- `uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py tests/test_rules_misc.py tests/test_rules_night_targets.py tests/test_llm_structured_output.py tests/test_llm_trigger.py`
- `uv run ruff check src tests`
- `uv run mypy`

最後に簡潔に報告すること:
- 何を変えたか
- どのファイルを変えたか
- 追加したテスト
- 実行した検証コマンドと結果
- 残課題があればその内容
```
