# `wolfbot` 2026-04-25 LLM CO Parser De-emphasis Update Prompt

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player が「占いCO」「霊媒CO」などの語を話題に出しただけの発言を、実際の CO と誤認しないようにすることです。強い熟練した人狼プレイヤーに近づけるには、壊れやすい CO parser の機械整理を信頼させるより、公開ログを文脈込みで読ませる方針を優先します。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player が、CO 語彙の単純出現だけで「その人が CO した」と誤認しないようにする。
- 現在の user context に出ている CO parser 由来の機械整理を削除または無効化し、LLM が公開ログを文脈として読む設計へ戻す。
- 熟練者らしく、明示的な自己役職宣言、対抗 CO、判定履歴、投票、噛み筋を総合して判断する prompt にする。

今回必ず対応すること:
1. `PLAYER_SPEECH` 内に `占いCO`、`霊媒CO`、`騎士CO` などの語が含まれるだけで CO 扱いしないこと。
2. `build_user_context()` から、CO parser 由来の `CO・判定の機械整理`、`盤面分類`、`役職推定メモ` を削除または非表示にすること。
3. 縄数・生存人数・PP/RPP 注意など、自然言語 parser に依存しない確定計算は残してよい。
4. system prompt または共通ルールに、「CO は本人が自分の役職として明確に宣言した場合のみ扱う」という方針を追加すること。
5. ゲームルール、DB schema、Discord command、状態遷移、勝敗判定、権限管理、recovery は変えないこと。

最重要ルール:
- まず `CLAUDE.md` と `src/wolfbot/llm/context_analysis.py`、`src/wolfbot/llm/prompt_builder.py`、`tests/test_llm_context_analysis.py`、`tests/test_llm_prompt_builder.py` を読むこと。
- Discord channel history を直接読んで prompt に入れてはいけない。
- LLM 文脈は既存どおり DB の public/private logs からだけ構築すること。
- 非狼に狼相方情報を漏らしてはいけない。
- 狂人に本物の人狼位置が見えている前提の情報を渡してはいけない。
- 変更は LLM prompt / user context / prompt tests 周辺に閉じること。

このリポジトリで確認済みの事実:
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md`。
- system prompt は `src/wolfbot/llm/prompt_builder.py::build_system_prompt()` が合成する。
- user context は `src/wolfbot/llm/prompt_builder.py::build_user_context()` が作る。
- `build_user_context()` は現在、`analyze_context()` と `render_context_analysis()` を通して CO parser 由来の機械整理を user context に差し込んでいる。
- 現在の `src/wolfbot/llm/context_analysis.py` は、`占いCO`、`霊媒CO`、`占い師です` などの正規表現を substring search で拾う。
- そのため、「占いCOが出たらどう見るか」「霊媒COについて考えたい」のような単なる言及・仮定・質問でも、発言者自身の CO と誤検出する可能性がある。
- `CLAUDE.md` では、共通推理ルールは `_build_game_rules_block()`、役職固有戦略は `_ROLE_STRATEGIES`、base framing / output format / hard invariants は `llm_system_prompt.md` に置く方針になっている。
- 人狼相方情報は `me.role is Role.WEREWOLF` の場合だけ user context に入る。
- LLM 文脈は DB の `public_logs` / `private_logs` から構築され、Discord channel history を直接読まない。

実装方針:
- `context_analysis.py` の CO / 判定 parser を完全削除するか、少なくとも `build_user_context()` から呼ばれない状態にする。
- `render_context_analysis()` を残す場合でも、CO parser 結果を user context に出さない。
- `calculate_rope_summary()` 相当の確定計算は、必要なら小さな helper として残す。
- `_build_game_rules_block()` に、次の趣旨を追加する:
  - `「占いCOが出たら」「霊媒COについて」「占いCOしている人をどう見るか」など、CO 語彙を話題にしているだけの発言は、その発言者自身の CO ではない。`
  - `本人が「私は占い師です」「占い師COします」「霊媒師として出ます」のように自分の役職として明確に宣言した場合だけ CO として扱う。`
  - `疑わしい場合は、公開ログの前後関係、主語、自分自身の宣言か他者への言及かを確認する。`
- user context には公開ログの原文を残し、LLM が文脈込みで判断できるようにする。

削除または非表示にする user context セクション:
- `## CO・判定の機械整理`
- `## 盤面分類`
- `## 役職推定メモ (公開情報ベース)`

残してよい user context セクション:
- 生存者 / 死亡者
- 現在フェイズ
- 人狼本人だけに見える仲間情報
- 私的メモ
- 公開ログ要約
- 自分の直近の発言
- 自然言語 parser に依存しない縄数・PP/RPP 注意

やってはいけないこと:
- 配役を変える
- ルールエンジンを変える
- 状態遷移を変える
- DB schema を増やす
- slash command を増やす
- Discord API から message history を直接 prompt に流す
- 非狼に相方情報を見せる
- 狂人に本物の狼位置が見えている前提で書く
- 誤検出しやすい CO parser 結果を「公開情報ベースの事実整理」として user context に出し続ける

テスト:
- `tests/test_llm_context_analysis.py` は、CO parser 削除または非表示化に合わせて削除・縮小・更新する。
- 追加または更新するテスト:
  - `占いCOが出たら考えたい` は発言者の占い師 CO として user context に出ない。
  - `霊媒COについてどう見る？` は発言者の霊媒師 CO として user context に出ない。
  - `私は占い師です` / `占い師COします` のような明示宣言は、system prompt の判断方針上は CO として扱うべき文面になっている。
  - `build_user_context()` に `CO・判定の機械整理`、`盤面分類`、`役職推定メモ` が含まれない。
  - 縄数ブロックを残す場合は、縄数表示が残ること。
  - 非狼 prompt に狼相方情報や wolf-only strategy が入らない既存保証を維持すること。
- 既存の role leak 防止テストを壊さない。

実行する検証コマンド:
- `uv run pytest tests/test_llm_context_analysis.py tests/test_llm_prompt_builder.py tests/test_llm_service.py`
- `uv run ruff check src tests`
- `uv run mypy`

受け入れ条件:
- LLM player が、CO 語彙の単純出現だけでその発言者を CO 者扱いしなくなる。
- user context に、誤検出した CO 表・盤面分類・役職推定メモが出ない。
- LLM player は公開ログ原文を読み、明示的な自己宣言と単なる言及を区別するよう促される。
- 熟練者らしい判断として、CO 履歴を使う場合も、主語・時系列・対抗有無・判定履歴・投票・噛み筋と合わせて読む。
- 既存の情報秘匿、role-specific strategy、persona 話法、structured output 制約は維持される。

最後に簡潔に報告すること:
- 何を変えたか
- どのファイルを変えたか
- 実行した検証コマンドと結果
- 残課題があればその内容
```
