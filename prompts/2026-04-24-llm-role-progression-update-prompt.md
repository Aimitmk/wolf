# `wolfbot` 2026-04-24 LLM Role Progression Update Prompt

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player の共通推理ルールに、占い師 CO / 霊媒師 CO の人数別進行論を追加することです。特に `3-1`、`2-2`、占い師 3 CO 時の占いローラー、霊媒師 2 CO 時の霊媒ローラーを、役職に関係なく全 LLM seat が参照できる共通ルールとして扱います。

参考として、一般的な人狼進行論を確認済みです。2-2 進行と霊媒ローラーは大阪人狼Lab.、9人村の 2-2 / 3-1 セオリーは「人狼殺9人村: 盤面セオリー」、3-1 の占いローラー / 黒ストップは複数の解説記事を参照してください。

- https://osaka-jinro-lab.com/article/2-2shinko/
- https://hikablog.work/jinrou/jinrou-9persons
- https://ameblo.jp/imahefu/entry-12641616227.html
- https://w.atwiki.jp/jinro-info/pages/234.html

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player が、占い師 CO / 霊媒師 CO の人数別進行をより自然に評価できるように system prompt を更新する。
- LLM player の共通ルールに、`3-1`、`2-2`、占い師 3 CO 時の占いローラー、霊媒師 2 CO 時の霊媒ローラーを追加する。
- 変更は LLM prompt / prompt test 周辺に閉じ、ゲームルール、DB schema、Discord command、状態遷移は変えない。

今回必ず対応すること:
1. 共通ルールとして、`3-1` は「占い師 CO 3 人 / 霊媒師 CO 1 人」の盤面であると明記すること。
2. `3-1` では、単独霊媒師を原則として真寄りの進行役として扱い、初日は占い師 CO から吊る進行を強く見ること。
3. 占い師が 3 人 CO した場合、占い CO 内に人外が 2 人いる可能性が高いため、占いローラーまたは黒ストップを基本進行として考えること。
4. 占いローラーでは、偽目・狼目・情報が落ちる位置から占い師 CO を吊り、処刑後の霊媒結果と占い結果・投票・襲撃の整合性を見ること。
5. 黒ストップでは、単独霊媒師の霊媒結果で占い師 CO に黒が出た場合に、残り占い師を即吊り切らずグレー精査へ移る選択肢を検討すること。
6. ただし黒ストップは絶対ではなく、真狼狼、霊媒偽、残り占い師の破綻、PP リスクなどが強い場合は占いローラー続行を検討すること。
7. `2-2` は「占い師 CO 2 人 / 霊媒師 CO 2 人」の盤面であると明記すること。
8. `2-2` では、占い師・霊媒師のどちらも確定しないため、霊媒ローラーまたは霊媒吊り切りを基本進行として扱うこと。
9. 霊媒師が 2 人 CO した場合、片方だけを根拠なく真置きせず、霊媒結果は騙り混じりの可能性を常に見ること。
10. 霊媒ローラーを始めた場合は、原則として完遂する。途中で止めるには、公開ログ上の強い破綻、襲撃、投票、占い結果など、通常より強い理由が必要だと明記すること。
11. 既存の「対抗なし単独 CO は真寄り」「過去に対抗 CO があれば現在 1 人だけでも自動真置きしない」「霊媒白は非人狼だけを示す」ルールと矛盾させないこと。
12. 既存の情報秘匿、role-specific strategy の分離、persona 話法 block、structured output 制約を壊さないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築とテストを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つこと。今回の変更でルールエンジンや状態遷移を変えない。
- LLM prompt 構築は `src/wolfbot/llm/prompt_builder.py` と `src/wolfbot/prompts/llm_system_prompt.md` 周辺に閉じること。
- `src/wolfbot/prompts/llm_system_prompt.md` の構造を大きく変えず、原則として既存の `{game_rules_block}` に含めること。
- user context に新しい CO 集計データを足さないこと。今回は LLM が公開ログを読むときの判断ルールとして明文化する。
- CO 履歴を機械的に集計する parser、DB schema、状態遷移、slash command は追加しないこと。
- Discord channel history を直接拾って prompt に入れてはいけない。LLM 文脈は既存どおり DB の public/private logs からだけ構築すること。
- `game_id` / `audience_seat` ベースの情報分離を壊さないこと。
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
- `_build_game_rules_block()` には、9 人村固定配役、勝利条件、秘匿情報の扱い、占い/霊媒の黒判定、NIGHT_0 ランダム白、襲撃失敗、騎士連続護衛禁止、候補トークン、CO 評価方針が含まれている。
- `_build_game_rules_block()` には、すでに「対抗なし単独 CO は真寄り」「単独 CO は絶対真確定ではない」「過去に対抗 CO があれば現在 1 人だけでも自動真置きしない」「死亡済み CO 者も比較対象にする」趣旨がある。
- `_build_game_rules_block()` には、すでに「霊媒白は本物の人狼ではないことだけを示す」「処刑された占い師 CO が霊媒白でも真占い師と矛盾しない」「霊媒黒なら人狼騙りと強く見てよい」趣旨がある。
- 現在の共通ルールには、`3-1`、`2-2`、占いローラー、黒ストップ、霊媒ローラー、霊媒ローラー完遂の進行論はまだ十分に固定されていない。
- `LLMAdapter._ask()` は `repo.load_public_logs(game.id, ...)` と `repo.load_private_logs_for_audience(game.id, audience_seat=player.seat_no, ...)` を使っており、DB 読み出しは `game_id` 単位にスコープされている。
- `build_user_context()` では、人狼相方情報は `me.role is Role.WEREWOLF` の場合だけ user context に入る。

参考にした一般的な進行論:
- `2-2`: 占い師と霊媒師が 2 人ずつ CO している盤面。霊媒師が 2 人いるため霊媒を確定させず、霊媒ローラー / 霊媒吊り切りが基本進行になりやすい。
- `3-1`: 占い師が 3 人、霊媒師が 1 人 CO している盤面。単独霊媒師を軸にし、占い師 CO から吊る進行が基本になりやすい。
- 占い師 3 CO: 真占い師 1 人に対して騙りが 2 人いるため、占いローラーで人外を落とす進行、または霊媒黒で止める黒ストップが候補になる。
- 霊媒師 2 CO: 真霊媒師 1 人に対して騙りが 1 人いるため、霊媒結果は確定情報として扱わず、ローラー完遂を基本にする。

実装要求

## 1. 共通ルール block に人数別進行論を追加する

必要な仕様:
- すべての LLM player が、役職に関係なく「占い師 CO 数」「霊媒師 CO 数」「対抗 CO の有無」を推理材料として扱うこと。
- `3-1` と `2-2` の意味を prompt 内で説明し、略語だけで終わらせないこと。
- 既存の単独 CO 評価ルールと整合させ、単独霊媒師は真寄りだが絶対確定ではない、2 CO 霊媒師は片方だけを根拠なく真置きしない、という区別を明確にすること。
- 既存の霊媒白 / 霊媒黒の解釈ルールと整合させ、黒ストップや霊媒ローラー判断に霊媒結果を使う場合も「霊媒が真なら」という条件を崩さないこと。

このタスクで固定する実装方針:
- `src/wolfbot/llm/prompt_builder.py` の `_build_game_rules_block()` に文面を追加すること。
- 既存の `{game_rules_block}` の差し込み構造を使い、`src/wolfbot/prompts/llm_system_prompt.md` の大きな構造変更は避けること。
- user context や service 層に CO 集計結果を足さないこと。
- role-specific strategy block には原則として触れないこと。今回の進行論は全 LLM player 共通の判断材料として扱う。

共通ルール block に必ず含める内容:
- `3-1` は「占い師 CO 3 人 / 霊媒師 CO 1 人」の盤面であること。
- `3-1` では、単独霊媒師は原則として真寄りの進行役として扱い、初日は占い師 CO から吊る進行を強く見ること。
- 占い師 3 CO では、占い CO 内に人外が 2 人いる可能性が高く、占いローラーまたは黒ストップを基本候補にすること。
- 占いローラーでは、偽目・狼目・情報が落ちる位置から占い師 CO を吊ること。
- 黒ストップでは、単独霊媒師の霊媒結果で占い師 CO に黒が出た場合、残り占い師を即吊り切らずグレー精査へ移る選択肢を検討すること。
- 黒ストップは絶対ではなく、真狼狼、霊媒偽、残り占い師の破綻、PP リスクなどが強い場合はローラー続行を検討すること。
- `2-2` は「占い師 CO 2 人 / 霊媒師 CO 2 人」の盤面であること。
- `2-2` では、占い師・霊媒師のどちらも確定しないため、霊媒ローラーまたは霊媒吊り切りを基本進行として扱うこと。
- 霊媒師 2 CO では、片方だけを根拠なく真置きせず、霊媒結果は騙り混じりの可能性を常に見ること。
- 霊媒ローラーを始めた場合は、原則として完遂すること。途中で止めるには通常より強い理由が必要であること。

推奨文面例:
- `3-1 (占い師 CO 3 人 / 霊媒師 CO 1 人) では、単独霊媒師を原則として真寄りの進行役として扱い、初日は占い師 CO から吊る進行を強く見る。`
- `占い師が 3 人 CO した場合、占い CO 内に人外が 2 人いる可能性が高い。占いローラーで確実に人外を落とすか、単独霊媒師の霊媒黒を見た時点で黒ストップしてグレー精査へ移るかを検討する。`
- `黒ストップは、霊媒師が真で、占い師の内訳が真狂狼に近いと判断できるときの選択肢であり、真狼狼や霊媒偽、残り占い師の破綻、PP リスクが強い場合はローラー続行を検討する。`
- `2-2 (占い師 CO 2 人 / 霊媒師 CO 2 人) では、占い師も霊媒師も確定しない。霊媒師 2 CO は騙り混じりとして扱い、霊媒ローラーまたは霊媒吊り切りを基本進行として考える。`
- `霊媒ローラーを始めた場合は原則として完遂する。途中で片方だけ残すには、公開ログ上の強い破綻、襲撃、投票、占い結果との整合性など、通常より強い理由が必要。`

## 2. 既存の CO 評価ルールとの矛盾を避ける

必要な仕様:
- `3-1` の単独霊媒師は、既存の「対抗なし単独 CO は真寄り」ルールに沿って扱う。
- ただし、単独霊媒師も絶対確定ではない。公開ログ上の破綻、投票矛盾、霊媒結果の矛盾、噛み筋との不整合があれば疑ってよい。
- `2-2` の霊媒師 2 CO は、既存の「対抗 CO 履歴があれば自動真置きしない」ルールに沿って扱う。
- 霊媒ローラー中に片方が死んで現在 1 人だけになっても、その残存霊媒師を「対抗なし単独 CO」として真置きしない。
- 霊媒結果の白 / 黒は、既存どおり「本物の人狼かどうか」だけを示す。特に霊媒白は役職名や村陣営確定を示さない。

このタスクで固定する実装方針:
- `_build_game_rules_block()` 内で、既存の CO 評価文面の近くに人数別進行論を置くこと。
- 文面が長くなりすぎる場合は、CO 評価方針を短い bullet 群に整理してよい。
- 既存テストが文字列断片に依存しているため、テストを新しい文面に合わせて更新すること。

## 3. 情報秘匿と prompt 分離を維持する

必要な仕様:
- 非人狼の prompt に狼相方情報が混ざってはいけない。
- 狼以外の role-specific strategy に、人狼専用の夜連携戦術が混ざってはいけない。
- 狂人には真の人狼位置を知らせない。
- 別 `game_id` の log は current game の prompt に混ざってはいけない。
- Discord の message history を直接拾って prompt に入れてはいけない。
- 進行論は「公開ログから読み取れる CO 状況に対する推理方針」であり、実際の役職内訳を LLM に漏らすものではない。

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
- CO parser や自動盤面分類器を追加する
- Discord API から message history を直接 prompt に流す
- 非狼に相方情報を見せる
- 狂人に本物の狼位置が見えている前提で書く
- 無関係な refactor を広げる

必要なテスト変更:

## `tests/test_llm_prompt_builder.py` を更新する
- `_build_game_rules_block()` に、`3-1` が「占い師 CO 3 人 / 霊媒師 CO 1 人」である趣旨が含まれること。
- `_build_game_rules_block()` に、`3-1` では単独霊媒師を真寄り進行役として扱い、占い師 CO から吊る趣旨が含まれること。
- `_build_game_rules_block()` に、占い師 3 CO では占いローラーまたは黒ストップを検討する趣旨が含まれること。
- `_build_game_rules_block()` に、黒ストップは絶対ではなく、真狼狼・霊媒偽・PP リスクなどではローラー続行を検討する趣旨が含まれること。
- `_build_game_rules_block()` に、`2-2` が「占い師 CO 2 人 / 霊媒師 CO 2 人」である趣旨が含まれること。
- `_build_game_rules_block()` に、`2-2` では霊媒ローラーまたは霊媒吊り切りを基本進行とする趣旨が含まれること。
- `_build_game_rules_block()` に、霊媒師 2 CO では霊媒結果を確定情報として扱わず、騙り混じりの可能性を見る趣旨が含まれること。
- `_build_game_rules_block()` に、霊媒ローラーを始めた場合は原則完遂し、途中停止には強い理由が必要という趣旨が含まれること。
- 既存の単独 CO、対抗 CO 履歴、霊媒白、霊媒黒に関するテストは壊さないこと。
- 既存の role-specific strategy leak 防止テストは壊さないこと。

## `tests/test_llm_service.py` を更新する
- `_CapturingDecider` を使い、`_ask()` が組み立てた `system_prompt` に更新後の `3-1` / `2-2` 進行論が含まれることを検証する。
- 占い師 3 CO 時の占いローラー / 黒ストップ方針が `system_prompt` に届くことを検証する。
- 霊媒師 2 CO 時の霊媒ローラー / ローラー完遂方針が `system_prompt` に届くことを検証する。
- 非狼の prompt に狼相方情報や狼専用 strategy が入らない既存保証を維持すること。

既存テスト群は壊さないこと:
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`

受け入れ条件:
- すべての LLM player が、`3-1` と `2-2` の基本進行を system prompt で受け取る。
- `3-1` では、単独霊媒師を真寄りの進行役として扱い、占い師 CO から吊る進行を強く見る prompt になっている。
- 占い師 3 CO では、占いローラーと黒ストップの意味、利点、例外が prompt に含まれている。
- `2-2` では、霊媒師を確定させず、霊媒ローラー / 霊媒吊り切りを基本進行として考える prompt になっている。
- 霊媒師 2 CO では、片方だけを根拠なく真置きせず、ローラー完遂を基本とする prompt になっている。
- 既存の単独 CO 評価、対抗 CO 履歴、霊媒白 / 霊媒黒の解釈と矛盾しない。
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
