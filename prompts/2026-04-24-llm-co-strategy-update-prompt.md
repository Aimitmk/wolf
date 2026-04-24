# `wolfbot` 2026-04-24 LLM CO Strategy Update Prompt

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、既存の LLM prompt 構築・情報秘匿・役職別 strategy block の設計を壊さずに、CO への扱いと役職ごとの立ち回り指針を補強することです。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player が、CO 状況をより自然に評価し、役職ごとの立ち回りを改善できるように system prompt を更新する。
- 特に「単独 CO の真寄り評価」「騎士の護衛成功後 CO」「人狼・狂人の騙り戦略」を強化する。
- 変更は LLM prompt / strategy 周辺に閉じ、ゲームルール、DB schema、Discord command、状態遷移は変えない。

今回必ず対応すること:
1. LLM player の共通ルールとして、特定役職の CO が 1 人だけで対抗 CO が出ていない場合、その CO 者を原則として真の役職者にかなり近い位置として扱うようにすること。
2. ただし単独 CO を絶対真確定にしすぎず、公開ログ上の破綻・投票矛盾・結果矛盾・噛み筋など強い反証がある場合は疑ってよい、と明記すること。
3. 騎士の役職 strategy として、護衛成功が発生したと判断できる朝には、護衛先を添えて騎士 CO する価値が高いことを明記すること。
4. 人狼と狂人の役職 strategy として、初日に占い師を騙る可能性を高めること。
5. 人狼と狂人の役職 strategy として、すでに対抗占い師が出ている場合は、翌日に霊媒師騙りまたは騎士騙りを検討し、CO 時に夜能力を使った想定の結果も添えるようにすること。
6. 人狼と狂人の役職 strategy として、対抗 CO するプレイヤーが合計 3 人以上に膨らむと、役職として出ていない人の白さが強まりやすいので、騙りすぎに注意することを明記すること。
7. 既存の情報秘匿、role-specific strategy の分離、persona 話法 block、structured output 制約を壊さないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築とテストを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つこと。今回の変更でルールエンジンや状態遷移を変えない。
- LLM prompt 構築は `src/wolfbot/llm/prompt_builder.py` と `src/wolfbot/prompts/llm_system_prompt.md` 周辺に閉じること。
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
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`

このリポジトリで確認済みの事実:
- `src/wolfbot/llm/prompt_builder.py` には、すでに `_build_game_rules_block()` と `_build_strategy_block(role)` がある。
- `src/wolfbot/prompts/llm_system_prompt.md` には、共通ルール、人格、話法、自分の役職、役職別の立ち回り指針、現在フェイズ、今回タスクの block がある。
- `build_system_prompt()` は seat ごと・呼び出しごとに system prompt を組み立てている。
- `LLMAdapter._ask()` は `repo.load_public_logs(game.id, ...)` と `repo.load_private_logs_for_audience(game.id, audience_seat=player.seat_no, ...)` を使っており、DB 読み出しは `game_id` 単位にスコープされている。
- `build_user_context()` では、人狼相方情報は `me.role is Role.WEREWOLF` の場合だけ user context に入る。
- 既存テストは、共通ルール block、役職別 strategy block、話法 block、role leak 防止をすでに検証している。
- 現在の strategy には、占い師 CO の対抗確認や騎士 CO 温存などの一般指針はあるが、「単独 CO を真寄りに扱う」「護衛成功後に護衛先込みで騎士 CO」「狼/狂人の騙り優先度」までは十分に固定されていない。

実装要求

## 1. 共通ルール block に CO 評価方針を追加する

必要な仕様:
- すべての LLM player が、役職に関係なく「CO 数と対抗 CO の有無」を推理材料として扱うこと。
- 特定の役職 CO が 1 人だけで、公開ログ上まだ対抗 CO が出ていない場合、その CO 者は原則として真の役職者にかなり近い位置として扱うこと。
- 単独 CO 者を、根拠なく疑って投票する行動を避けること。
- 単独 CO は「絶対真確定」ではなく、破綻や強い矛盾があれば疑ってよいこと。

このタスクで固定する実装方針:
- `src/wolfbot/llm/prompt_builder.py` の `_build_game_rules_block()` に CO 評価方針を追加すること。
- `src/wolfbot/prompts/llm_system_prompt.md` の構造は大きく変えなくてよい。既存の `{game_rules_block}` に含める形を優先すること。
- user context に新しい集計データを足さないこと。今回は prompt 文面の強化に留める。

共通ルール block に必ず含める内容:
- 「占い師 / 霊媒師 / 騎士などの役職 CO が 1 人だけで、対抗 CO が公開ログ上に出ていない場合、その CO 者は原則として真寄りに扱う」
- 「単独 CO 者を処刑候補にするには、判定矛盾、発言破綻、投票矛盾、噛み筋との不整合など、通常より強い根拠が必要」
- 「対抗 CO が出た場合は、結果・時系列・投票・襲撃結果との整合性で比較する」

## 2. 騎士 strategy を護衛成功後 CO 寄りに更新する

必要な仕様:
- 騎士は普段は護衛先を不用意に公開しない。
- ただし、夜に犠牲者が出なかった朝は護衛成功の可能性があり、その場合は騎士 CO の価値が上がる。
- 護衛成功を主張して CO する場合は、護衛先もセットで公開する。
- 護衛成功時の CO は、守った相手を真寄り・白寄りに置く材料になり、村の推理を進められる。

このタスクで固定する実装方針:
- `_ROLE_STRATEGIES[Role.KNIGHT]` を更新すること。
- 既存の「護衛先を不用意に公開しない」方針は残しつつ、「平和な朝 / 犠牲者なし」の場合は例外として CO 価値が高い、と明確に分けること。
- 「同じ相手を連続で護衛できない」既存ルールは維持すること。

騎士 strategy に必ず含める内容:
- 「平和な朝で、自分の護衛先が襲撃先だった可能性が高い場合は、騎士 CO を検討する」
- 「護衛成功を理由に CO するときは、必ず護衛先を添える」
- 「護衛先を公開することで噛み筋ヒントも与えるため、通常時の無意味な CO は避ける」

## 3. 人狼 strategy を騙り重視に更新する

必要な仕様:
- 人狼は初日に占い師騙りを積極的に検討する。
- 占い師騙りでは、公開ログと矛盾しない白 / 黒結果を出す。
- すでに別の占い師 CO が出ている場合、翌日に霊媒師騙りまたは騎士騙りを検討する。
- 霊媒師騙りでは、前日処刑者に対する霊媒結果を添えて CO する。
- 騎士騙りでは、夜に護衛した想定の護衛先と、平和が出たなら護衛成功主張を添えて CO する。
- 騙りすぎると、役職 CO していない位置の白さが強まるので、CO 数の膨らみを警戒する。

このタスクで固定する実装方針:
- `_ROLE_STRATEGIES[Role.WEREWOLF]` を更新すること。
- 既存の人狼専用語彙 `相方` / `襲撃先を揃える` は人狼 strategy のみに残し、他役職へ漏らさないこと。
- 人狼 strategy に入れる内容は人狼本人向けなので、相方連携や騙り方針を書いてよい。

人狼 strategy に必ず含める内容:
- 「day 1 は占い師騙りを積極的に検討する」
- 「すでに対抗占い師がいる場合、day 2 以降に霊媒師騙りまたは騎士騙りを検討する」
- 「霊媒師騙りでは、夜に能力を使った想定で処刑者への霊媒結果を添える」
- 「騎士騙りでは、夜に能力を使った想定で護衛先と、必要なら護衛成功主張を添える」
- 「役職 CO / 対抗 CO が合計 3 人以上になると、役職 CO していない人が白く見えやすいので、騙りすぎに注意する」

## 4. 狂人 strategy を騙り重視に更新する

必要な仕様:
- 狂人は人狼勝利を助けるため、初日に占い師騙りを積極的に検討する。
- 狂人は真の人狼位置を知らない。人狼位置を知っている前提の発言や、相方連携のような指針を入れてはいけない。
- すでに別の占い師 CO が出ている場合、翌日に霊媒師騙りまたは騎士騙りを検討する。
- 霊媒師騙りでは、前日処刑者に対する霊媒結果を添えて CO する。
- 騎士騙りでは、夜に護衛した想定の護衛先と、平和が出たなら護衛成功主張を添えて CO する。
- 騙りすぎると、役職 CO していない位置の白さが強まるので、CO 数の膨らみを警戒する。

このタスクで固定する実装方針:
- `_ROLE_STRATEGIES[Role.MADMAN]` を更新すること。
- 狂人 strategy に `相方` / `襲撃先を揃える` のような人狼専用連携語彙を入れないこと。
- 狂人 strategy は「狼位置を知らないが、村の情報整理を乱すために騙る」という方針にすること。

狂人 strategy に必ず含める内容:
- 「day 1 は占い師騙りを積極的に検討する」
- 「すでに対抗占い師がいる場合、day 2 以降に霊媒師騙りまたは騎士騙りを検討する」
- 「霊媒師騙りでは、夜に能力を使った想定で処刑者への霊媒結果を添える」
- 「騎士騙りでは、夜に能力を使った想定で護衛先と、必要なら護衛成功主張を添える」
- 「役職 CO / 対抗 CO が合計 3 人以上になると、役職 CO していない人が白く見えやすいので、騙りすぎに注意する」
- 「本物の人狼位置を知っている前提で話してはならない」

## 5. 既存の情報秘匿と prompt 分離を維持する

必要な仕様:
- 非人狼の prompt に狼相方情報が混ざってはいけない。
- 狼以外の role-specific strategy に、人狼専用の夜連携戦術が混ざってはいけない。
- 狂人には真の人狼位置を知らせない。
- 別 `game_id` の log は current game の prompt に混ざってはいけない。
- Discord の message history を直接拾って prompt に入れてはいけない。

このタスクで固定する作業:
- `LLMAdapter._ask()` の DB ベースの文脈構築は維持すること。
- `build_user_context()` の現在のスコープ分離は壊さないこと。
- system prompt の強化で role leak が起きないよう、既存テストを更新 / 補強すること。

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
- `_build_game_rules_block()` に単独 CO の真寄り評価ルールが含まれること。
- `_build_game_rules_block()` に「単独 CO 者を疑うには強い矛盾が必要」という趣旨が含まれること。
- `Role.KNIGHT` の strategy に、平和な朝 / 護衛成功時の騎士 CO と護衛先公開の指針が含まれること。
- `Role.WEREWOLF` の strategy に、day 1 占い師騙り、day 2 以降の霊媒師 / 騎士騙り、CO 数 3 人以上への警戒が含まれること。
- `Role.MADMAN` の strategy に、day 1 占い師騙り、day 2 以降の霊媒師 / 騎士騙り、CO 数 3 人以上への警戒が含まれること。
- `Role.MADMAN` の strategy に「本物の人狼位置を知っている前提で話してはならない」趣旨が残っていること。
- 既存の cross-role leak テストを新しい文面に合わせて更新すること。
- `相方` / `襲撃先を揃える` などの人狼専用連携語彙が、人狼以外の strategy に出ないことを維持すること。

## `tests/test_llm_service.py` を更新する
- `_CapturingDecider` を使い、`_ask()` が組み立てた `system_prompt` に共通 CO 評価方針が含まれることを検証する。
- 騎士 seat の `system_prompt` に護衛成功後 CO 方針が含まれることを検証する。
- 人狼 seat の `system_prompt` に騙り strategy が含まれることを検証する。
- 狂人 seat の `system_prompt` に騙り strategy が含まれ、かつ人狼専用連携語彙が含まれないことを検証する。
- 非狼の prompt に狼相方情報や狼専用 strategy が入らない既存保証を維持すること。

既存テスト群は壊さないこと:
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`

受け入れ条件:
- 単独の占い師 CO が、対抗なしにもかかわらず LLM に根拠なく疑われて投票される傾向が下がる prompt になっている。
- すべての LLM player が、対抗なし単独 CO を真寄りに扱う共通方針を system prompt で受け取る。
- 騎士 LLM は、護衛成功が疑われる平和な朝に、護衛先を添えて CO する選択肢を強く認識する。
- 人狼 / 狂人 LLM は、初日占い師騙りと、翌日以降の霊媒師 / 騎士騙りをより積極的に検討する。
- 人狼 / 狂人 LLM は、CO 数が増えすぎることで非 CO 位置が白くなるリスクを認識する。
- 狂人には真の人狼位置や相方連携情報が漏れない。
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
