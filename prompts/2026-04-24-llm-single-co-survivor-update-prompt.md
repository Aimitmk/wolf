# `wolfbot` 2026-04-24 LLM Single CO Survivor Update Prompt

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player の共通推理ルールである CO 評価方針を精密化することです。特に「対抗 CO が一度も出ていない単独 CO」と、「過去に対抗 CO が出たあと生存者が 1 人だけになった CO」を明確に区別します。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player が、役職 CO の履歴をより正確に評価できるように system prompt を更新する。
- 「対抗 CO が一度も出ていない単独 CO」は原則として真寄りに扱う。
- ただし、過去に同じ役職 CO が 2 人以上存在した場合は、処刑・襲撃などで現在 1 人だけ残っていても、その残存 CO 者を自動的に真の役職者とはみなさない。
- 変更は LLM prompt / prompt test 周辺に閉じ、ゲームルール、DB schema、Discord command、状態遷移は変えない。

今回必ず対応すること:
1. LLM player の共通ルールとして、占い師・霊媒師・騎士などの特定役職 CO が 1 人だけで、公開ログ上その役職への対抗 CO が一度も出ていない場合、その CO 者を原則として真の役職者にかなり近い位置として扱うようにすること。
2. 単独 CO 者を根拠なく疑って投票しないことを明記すること。
3. 単独 CO は絶対真確定ではなく、発言破綻、投票矛盾、判定結果の矛盾、噛み筋との不整合など強い反証がある場合は疑ってよい、と明記すること。
4. 過去に同じ役職 CO が 2 人以上出ていた場合は、対抗者が処刑・襲撃・その他の理由で死亡して現在 1 人だけが残っていても、その残存 CO 者を「対抗なし単独 CO」として扱わないこと。
5. 対抗 CO 履歴がある役職については、生存中の CO 者が 1 人だけでも、過去の対抗 CO 者の発言、判定結果、投票、死亡タイミング、襲撃結果との整合性で真偽を比較するように明記すること。
6. 既存の情報秘匿、role-specific strategy の分離、persona 話法 block、structured output 制約を壊さないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築とテストを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つこと。今回の変更でルールエンジンや状態遷移を変えない。
- LLM prompt 構築は `src/wolfbot/llm/prompt_builder.py` と `src/wolfbot/prompts/llm_system_prompt.md` 周辺に閉じること。
- `src/wolfbot/prompts/llm_system_prompt.md` の構造を大きく変えず、原則として既存の `{game_rules_block}` に含めること。
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
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/llm/personas.py`
- `src/wolfbot/services/llm_service.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`

このリポジトリで確認済みの事実:
- `src/wolfbot/llm/prompt_builder.py` には、すでに `_build_game_rules_block()` と `_build_strategy_block(role)` がある。
- `src/wolfbot/prompts/llm_system_prompt.md` には、共通ルール、人格、話法、自分の役職、役職別の立ち回り指針、現在フェイズ、今回タスクの block がある。
- `build_system_prompt()` は seat ごと・呼び出しごとに system prompt を組み立てている。
- `_build_game_rules_block()` には、すでに「特定役職の CO が 1 人だけで対抗 CO がまだ出ていない場合、その単独 CO 者を真寄りに扱う」趣旨の文面がある。
- しかし現状の文面では、「過去に対抗 CO が出たあと、吊りや噛みで現在 1 人だけ残った場合は単独 CO 扱いしない」という条件が十分に明示されていない。
- `tests/test_llm_prompt_builder.py` には、共通 CO 評価方針、強い矛盾がある場合の疑い、対抗 CO 比較軸を確認するテストがすでにある。
- `tests/test_llm_service.py` には、`LLMAdapter._ask()` が組み立てた `system_prompt` に共通 CO 評価方針が含まれることを確認するテストがすでにある。
- `LLMAdapter._ask()` は `repo.load_public_logs(game.id, ...)` と `repo.load_private_logs_for_audience(game.id, audience_seat=player.seat_no, ...)` を使っており、DB 読み出しは `game_id` 単位にスコープされている。
- `build_user_context()` では、人狼相方情報は `me.role is Role.WEREWOLF` の場合だけ user context に入る。

実装要求

## 1. 共通ルール block の CO 評価方針を更新する

必要な仕様:
- すべての LLM player が、役職に関係なく「CO 数」「対抗 CO の有無」「過去の CO 履歴」を推理材料として扱うこと。
- 占い師・霊媒師・騎士などの特定役職 CO が 1 人だけで、公開ログ上その役職への対抗 CO が一度も出ていない場合、その CO 者は原則として真の役職者にかなり近い位置として扱うこと。
- 単独 CO 者を、根拠なく疑って投票する行動を避けること。
- 単独 CO は絶対真確定ではなく、強い反証があれば疑ってよいこと。
- 過去に同じ役職 CO が 2 人以上存在した場合、現在の生存 CO 者が 1 人だけになっても「対抗なし単独 CO」とは扱わないこと。
- 対抗 CO 履歴がある役職では、死亡済みの CO 者も推理対象として保持し、残存 CO 者を自動的に真置きしないこと。

このタスクで固定する実装方針:
- `src/wolfbot/llm/prompt_builder.py` の `_build_game_rules_block()` に文面を追加または既存文面を置き換えること。
- 既存の `{game_rules_block}` の差し込み構造を使い、`src/wolfbot/prompts/llm_system_prompt.md` の大きな構造変更は避けること。
- CO 履歴を機械的に集計する parser や DB schema は追加しないこと。今回は LLM が公開ログを読むときの判断ルールとして明文化する。
- 既存の role-specific strategy block には原則として触れないこと。必要がなければ `_ROLE_STRATEGIES` は変更しない。

共通ルール block に必ず含める内容:
- 「占い師・霊媒師・騎士などの役職 CO が 1 人だけで、同じ役職への対抗 CO が公開ログ上まだ一度も出ていない場合、その CO 者は原則として真寄りに扱う」
- 「単独 CO 者を処刑候補にするには、判定矛盾、発言破綻、投票矛盾、噛み筋との不整合など、通常より強い根拠が必要」
- 「同じ役職 CO が過去に 2 人以上出たことがある場合、吊り・噛み・死亡で現在 1 人だけ残っていても、その残存 CO 者を対抗なし単独 CO として真置きしない」
- 「対抗 CO 履歴がある場合は、死亡済み CO 者と生存 CO 者の両方について、判定結果・発言の時系列・投票・襲撃結果・死亡タイミングとの整合性で比較する」

推奨文面例:
- `特定役職 (占い師・霊媒師・騎士) の CO が公開ログ上その役職で 1 人だけで、同じ役職への対抗 CO が一度も出ていない場合、その CO 者は原則として真の役職者にかなり近い位置として扱う。根拠なくその CO 者を処刑候補にしない。`
- `ただし「現在生存している CO 者が 1 人だけ」というだけでは単独 CO 扱いしない。同じ役職 CO が過去に 2 人以上出たことがある場合、対抗者が処刑・襲撃などで死亡していても、残った CO 者を自動的に真置きしない。`
- `対抗 CO 履歴がある役職では、死亡済み CO 者も含め、判定結果・発言時系列・投票・襲撃結果・死亡タイミングとの整合性で真偽を比較する。`

## 2. 情報秘匿と prompt 分離を維持する

必要な仕様:
- 非人狼の prompt に狼相方情報が混ざってはいけない。
- 狼以外の role-specific strategy に、人狼専用の夜連携戦術が混ざってはいけない。
- 狂人には真の人狼位置を知らせない。
- 別 `game_id` の log は current game の prompt に混ざってはいけない。
- Discord の message history を直接拾って prompt に入れてはいけない。

このタスクで固定する作業:
- `LLMAdapter._ask()` の DB ベースの文脈構築は維持すること。
- `build_user_context()` の現在のスコープ分離は壊さないこと。
- system prompt の共通ルール文面だけを強化し、role leak が起きないようにすること。

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
- `_build_game_rules_block()` に、対抗 CO が一度も出ていない単独 CO を真寄りに扱うルールが含まれること。
- `_build_game_rules_block()` に、単独 CO 者を疑うには強い矛盾が必要という趣旨が含まれること。
- `_build_game_rules_block()` に、過去に同じ役職 CO が 2 人以上出た場合は、現在 1 人だけ残っていても単独 CO 扱いしない趣旨が含まれること。
- `_build_game_rules_block()` に、死亡済み CO 者も含めて判定結果・時系列・投票・襲撃結果・死亡タイミングとの整合性で比較する趣旨が含まれること。
- 既存の role-specific strategy leak 防止テストは壊さないこと。

## `tests/test_llm_service.py` を更新する
- `_CapturingDecider` を使い、`_ask()` が組み立てた `system_prompt` に更新後の共通 CO 評価方針が含まれることを検証する。
- 特に「現在 1 人だけ残った CO 者でも、過去に対抗 CO があれば自動真置きしない」趣旨が `system_prompt` に届くことを検証する。
- 非狼の prompt に狼相方情報や狼専用 strategy が入らない既存保証を維持すること。

既存テスト群は壊さないこと:
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`

受け入れ条件:
- 単独の占い師 CO が、対抗なしにもかかわらず LLM に根拠なく疑われて投票される傾向が下がる prompt になっている。
- すべての LLM player が、対抗なし単独 CO を真寄りに扱う共通方針を system prompt で受け取る。
- ただし、同じ役職 CO が過去に 2 人以上出た盤面では、生存 CO 者が 1 人だけでも自動的に真置きしない。
- 対抗 CO 履歴がある役職では、死亡済み CO 者も含めた履歴比較を促す prompt になっている。
- user context の秘匿範囲を広げず、system prompt 強化だけで判断方針を改善する。

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
