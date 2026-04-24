# `wolfbot` 2026-04-24 LLM Medium/Seer White Inference Update Prompt

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player、とくに霊媒師が「処刑された占い師 CO に霊媒結果で白が出た」盤面を誤読しないようにすることです。霊媒結果の白は「本物の人狼ではない」ことだけを示し、占い師 CO が偽だったことを意味しません。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player が、霊媒結果「人狼ではありませんでした」を正しく推理に使えるように system prompt / role strategy を更新する。
- 特に、処刑された占い師 CO が霊媒結果で白だった場合、その結果だけを理由に占い師 CO を偽扱いしないようにする。
- 変更は LLM prompt / prompt test 周辺に閉じ、ゲームルール、DB schema、Discord command、状態遷移は変えない。

今回必ず対応すること:
1. 霊媒結果「人狼ではありませんでした」は、対象が `Role.WEREWOLF` ではないことだけを示す、と LLM prompt に明記すること。
2. 処刑された占い師 CO が霊媒結果で白だった場合、それは真占い師と矛盾しない、と明記すること。
3. 霊媒白だけを理由に、その占い師 CO を偽扱いしてはいけない、と明記すること。
4. 処刑された占い師 CO を偽視する場合は、対抗 CO、占い結果の破綻、発言時系列、投票矛盾、襲撃結果、死亡タイミングなどの整合性を根拠にすること。
5. 霊媒白の占い師 CO は、真占い師・狂人・その他の非人狼騙りの可能性を区別して考えること。
6. 霊媒黒だった場合だけ、その占い師 CO は本物の占い師ではなく人狼の騙りだったと強く判断できる、と明記すること。
7. 既存の情報秘匿、role-specific strategy の分離、persona 話法 block、structured output 制約を壊さないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築とテストを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つこと。今回の変更でルールエンジンや状態遷移を変えない。
- 占い師 / 霊媒師の実判定ロジックは変更しない。すでに `domain/rules.py::is_detected_as_wolf(role)` と `medium_detection()` は `Role.WEREWOLF` だけを黒扱いする実装になっている。
- LLM prompt 構築は `src/wolfbot/llm/prompt_builder.py` と `src/wolfbot/prompts/llm_system_prompt.md` 周辺に閉じること。
- user context に新しい CO 集計データを足さないこと。今回は prompt 文面の強化に留める。
- Discord channel history を直接拾って prompt に入れてはいけない。LLM 文脈は既存どおり DB の public/private logs からだけ構築すること。
- `game_id` / `audience_seat` ベースの情報分離を壊さないこと。
- DB schema は変更しないこと。
- slash command は追加しないこと。
- 既存の advance loop、`WAITING_HOST_DECISION`、recovery、fire-and-forget の設計を壊さないこと。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/domain/rules.py`
- `src/wolfbot/domain/state_machine.py`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/llm/personas.py`
- `src/wolfbot/services/llm_service.py`
- `tests/test_rules_misc.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`

このリポジトリで確認済みの事実:
- `src/wolfbot/domain/rules.py::is_detected_as_wolf(role)` は `role is Role.WEREWOLF` だけを `True` にする。
- `src/wolfbot/domain/rules.py::medium_detection(executed_player)` は `is_detected_as_wolf(executed_player.role)` を返す。
- `tests/test_rules_misc.py` では、狂人と占い師が霊媒結果で `False`、つまり「人狼ではない」扱いになることが固定されている。
- `src/wolfbot/domain/state_machine.py` の霊媒結果文面は、黒なら `霊媒結果: <名前> は 人狼 でした。`、白なら `霊媒結果: <名前> は 人狼ではありませんでした。` になっている。
- `src/wolfbot/llm/prompt_builder.py::_build_game_rules_block()` には、すでに「占い師と霊媒師の判定は本物の人狼だけを黒と表示し、狂人は黒判定されない」という共通ルールがある。
- `Role.MEDIUM` の strategy には、すでに「処刑結果と占い師の主張・投票の流れを照合」「処刑された相手が狂人でも霊媒結果は白」「白結果だけでは村置き確定にはならない」という趣旨がある。
- ただし現状の prompt では、「処刑された占い師 CO に霊媒白が出た場合、その白結果は占い師 CO 偽の根拠ではない」という推理上の注意が十分に明示されていない。
- そのため LLM 霊媒師が、占い師 CO を吊った翌日に「霊媒結果で人狼ではなかったので、その占い師は偽だった」と誤った推理をする可能性がある。

実装要求

## 1. 共通ルール block に霊媒白の意味を追加する

必要な仕様:
- すべての LLM player が、霊媒結果の白を「本物の人狼ではない」という二値情報として扱うこと。
- 霊媒白は役職名を否定しない。対象が占い師 CO だった場合も、真占い師である可能性と矛盾しないこと。
- 霊媒白だけで「占い師 CO は偽」と判断しないこと。
- 霊媒黒なら、対象は本物の人狼なので、その占い師 CO は真占い師ではなく人狼の騙りだったと判断できること。

このタスクで固定する実装方針:
- `src/wolfbot/llm/prompt_builder.py` の `_build_game_rules_block()` に、霊媒白の解釈ルールを追加する。
- 既存の `{game_rules_block}` の差し込み構造を使い、`src/wolfbot/prompts/llm_system_prompt.md` の大きな構造変更は避けること。
- CO 履歴を機械的に集計する parser や DB schema は追加しないこと。今回は LLM が公開ログと私的霊媒結果を読むときの判断ルールとして明文化する。

共通ルール block に必ず含める内容:
- 「霊媒結果の白は、その人物が本物の人狼ではないことだけを示す」
- 「霊媒結果は役職名を明かさず、占い師・霊媒師・騎士・村人・狂人のどれかまでは分からない」
- 「処刑された占い師 CO が霊媒白でも、真占い師だった可能性と矛盾しない」
- 「霊媒白だけを理由に占い師 CO を偽扱いしない」
- 「霊媒黒だった場合は、その占い師 CO は本物の占い師ではなく人狼の騙りだったと強く見てよい」

推奨文面例:
- `霊媒結果の「人狼ではありませんでした」は、その人物が本物の人狼ではないことだけを示す。役職名までは分からない。`
- `処刑された占い師 CO が霊媒白だった場合、その結果は真占い師である可能性と矛盾しない。霊媒白だけを理由に、その占い師 CO を偽扱いしない。`
- `処刑された占い師 CO が霊媒黒だった場合は、その人物は本物の人狼なので、真占い師ではなく人狼の騙りだったと強く判断してよい。`

## 2. 霊媒師 strategy を補強する

必要な仕様:
- 霊媒師 LLM は、自分の霊媒結果が占い師 CO の真偽へ与える影響を正しく整理すること。
- 占い師 CO 処刑後に霊媒白が出た場合、まず「人狼ではなかった」という事実だけを共有し、占い師 CO の真偽は別材料で判断すること。
- 偽の可能性を追う場合は、狂人など非狼騙りの可能性も含めて整理すること。

このタスクで固定する実装方針:
- `src/wolfbot/llm/prompt_builder.py` の `_ROLE_STRATEGIES[Role.MEDIUM]` に、占い師 CO 処刑後の霊媒白を誤読しないための文面を追加する。
- 人狼や狂人専用の騙り strategy を霊媒師 strategy に混ぜないこと。
- 霊媒師に見えていない人狼位置や夜行動を事実として扱わせる文面にしないこと。

霊媒師 strategy に必ず含める内容:
- 「占い師 CO を処刑して霊媒白だった場合、それは占い師 CO 偽の証明ではない」
- 「真占い師だった可能性、狂人など非狼騙りだった可能性を分けて考える」
- 「偽視するなら、対抗 CO、判定結果の破綻、発言時系列、投票、襲撃結果、死亡タイミングとの整合性で判断する」

推奨文面例:
- `占い師 CO を処刑して霊媒結果が白だった場合、それは占い師 CO 偽の証明ではない。真占い師だった可能性と、狂人など非狼の騙りだった可能性を分けて整理する。`
- `占い師 CO を偽視する場合は、霊媒白そのものではなく、対抗 CO、占い結果の破綻、発言時系列、投票、襲撃結果、死亡タイミングとの整合性を根拠にする。`

## 3. 情報秘匿と prompt 分離を維持する

必要な仕様:
- 非人狼の prompt に狼相方情報が混ざってはいけない。
- 狼以外の role-specific strategy に、人狼専用の夜連携戦術が混ざってはいけない。
- 狂人には真の人狼位置を知らせない。
- 別 `game_id` の log は current game の prompt に混ざってはいけない。
- Discord の message history を直接拾って prompt に入れてはいけない。

このタスクで固定する作業:
- `LLMAdapter._ask()` の DB ベースの文脈構築は維持すること。
- `build_user_context()` の現在のスコープ分離は壊さないこと。
- system prompt と role strategy の文面だけを強化し、role leak が起きないようにすること。

やってはいけないこと:
- 配役を変える
- ルールエンジンを変える
- 状態遷移を変える
- DB schema を増やす
- slash command を増やす
- Discord API から message history を直接 prompt に流す
- 非狼に相方情報を見せる
- 狂人に本物の狼位置が見えている前提で書く
- 無関係な refactor を広げる

必要なテスト変更:

## `tests/test_llm_prompt_builder.py` を更新する
- `_build_game_rules_block()` に、霊媒白は「本物の人狼ではない」ことだけを示す趣旨が含まれること。
- `_build_game_rules_block()` に、処刑された占い師 CO が霊媒白でも真占い師の可能性と矛盾しない趣旨が含まれること。
- `_build_game_rules_block()` に、霊媒白だけを理由に占い師 CO を偽扱いしない趣旨が含まれること。
- `_build_game_rules_block()` に、占い師 CO が霊媒黒なら人狼騙りと強く判断できる趣旨が含まれること。
- `_build_strategy_block(Role.MEDIUM)` に、占い師 CO 処刑後の霊媒白を偽証明として扱わない文面が含まれること。
- `_build_strategy_block(Role.MEDIUM)` に、真占い師だった可能性と狂人など非狼騙りだった可能性を分けて整理する文面が含まれること。
- 既存の role-specific strategy leak 防止テストは壊さないこと。

## `tests/test_llm_service.py` を更新する
- `_CapturingDecider` を使い、`_ask()` が組み立てた `system_prompt` に更新後の霊媒白解釈ルールが含まれることを検証する。
- 特に霊媒師 role の `system_prompt` に、「占い師 CO を処刑して霊媒白だった場合、それだけで偽扱いしない」趣旨が届くことを検証する。
- 非狼の prompt に狼相方情報や狼専用 strategy が入らない既存保証を維持すること。

既存テスト群は壊さないこと:
- `tests/test_rules_misc.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`

受け入れ条件:
- LLM 霊媒師が、処刑された占い師 CO への霊媒結果が「人狼ではありませんでした」だったとき、その占い師を霊媒白だけで偽扱いしない prompt になっている。
- すべての LLM player が、霊媒白は「非人狼」だけを示し、役職名や真偽までは確定しない共通方針を system prompt で受け取る。
- 霊媒師 LLM は、占い師 CO の真偽を判断する際に、霊媒結果だけでなく CO 履歴、占い結果、発言時系列、投票、襲撃結果、死亡タイミングとの整合性を使う。
- 霊媒黒だった場合は、対象が本物の人狼だったため、占い師 CO なら人狼騙りと強く判断できる。
- user context の秘匿範囲を広げず、system prompt / role strategy 強化だけで判断方針を改善する。

実行する検証コマンド:
- `uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py tests/test_rules_misc.py tests/test_llm_structured_output.py`
- `uv run ruff check src tests`
- `uv run mypy`

最後に簡潔に報告すること:
- 何を変えたか
- どのファイルを変えたか
- 実行した検証コマンドと結果
- 残課題があればその内容
```
