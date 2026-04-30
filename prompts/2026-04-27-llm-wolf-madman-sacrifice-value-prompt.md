# `wolfbot` 2026-04-27 LLM 人狼・狂人の自己犠牲価値強化プロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player を強い熟練した人狼プレイヤーとして振る舞わせることです。特に、狂人は自分が吊られるだけでも人狼陣営の縄消費・議論撹乱として仕事を果たせる場面があること、人狼は自分が 1 人と刺し違えるだけでも人狼陣営の勝ち筋を残す仕事を果たせる場面があることを、役職別 strategy に明確に含めます。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player を、より強い熟練した 9 人村プレイヤーとして振る舞わせる。
- 狂人 strategy に、自分が吊られるだけでも人狼陣営の仕事を果たせる場面があることを明文化する。
- 人狼 strategy に、重要人物 1 人と刺し違えるだけでも人狼陣営の仕事を果たせる場面があることを明文化する。
- 変更は LLM prompt / prompt tests 周辺に閉じる。ゲームルール、DB schema、Discord command、状態遷移、権限管理、復旧処理は変えない。

今回必ず対応すること:
1. `Role.MADMAN` の strategy に、狂人は縄消費・議論撹乱・真役職巻き込みによって、自分が吊られるだけでも人狼陣営の仕事を果たしたことになる場面がある、と明記する。
2. `Role.WEREWOLF` の strategy に、人狼は生存価値が高い一方で、真役職・確白級・強い進行役・相方を疑うキープレイヤーなど 1 人と刺し違えるだけでも人狼陣営の仕事を果たしたことになる場面がある、と明記する。
3. ただし、どちらも無意味な自爆や根拠のない自吊り誘導を推奨しない。熟練者として、縄数、残り人狼数、相方の位置、PP/RPP、CO 数、真役職の信用、翌日の盤面を見て交換価値を判断する文面にする。
4. 狂人には、本物の人狼位置を知らない前提を維持させる。`相方`、`襲撃先を揃える`、本物の狼位置を知っている前提の語彙を混ぜない。
5. 人狼には、公開発言で `相方` 語彙や私的情報を漏らさない既存方針を維持させる。
6. runtime template、structured output schema、LLM provider 実装、user context、CO parser、DB schema は変更しない。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築、役職別 strategy の分離、cross-leak tests を把握してから修正すること。
- `domain/` は純粋ロジックのまま保つこと。今回の変更でルールエンジンや状態遷移を変えない。
- LLM prompt 構築の変更は原則として `src/wolfbot/llm/prompt_builder.py` の `_ROLE_STRATEGIES` と、そのテストに閉じること。
- `src/wolfbot/prompts/llm_system_prompt.md` の基本構造は変更しない。今回の内容は role-specific strategy であり、共通ルールへ長文を置かない。
- user context に新しい推理ブロック、CO 集計、縄数自動計算、役職推定結果を足さない。
- Discord channel history を直接 prompt に入れない。LLM 文脈は既存どおり DB の public/private logs からだけ構築する。
- `game_id` / `audience_seat` ベースの情報分離を壊さない。
- slash command、配役、勝利条件、DB schema は変更しない。
- 無関係な大規模 refactor をしない。
- 作業ツリーに既存の未コミット変更や未追跡ファイルがあっても、今回の目的に無関係なら戻さない。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/services/llm_service.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`

このリポジトリで確認済みの事実:
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md` で、実際の共通ルール・役職別 strategy は `src/wolfbot/llm/prompt_builder.py` が組み立てている。
- `_build_game_rules_block()` は全 LLM seat に届く共通ルールで、`_ROLE_STRATEGIES[role]` は role ごとの tactical hints として差し込まれる。
- `Role.WEREWOLF` の strategy には、相方との襲撃先統一、視点漏れ防止、騙り/潜伏の条件付き判断、身内票、ライン切り、噛み筋、護衛読みなどが既に含まれている。
- `Role.MADMAN` の strategy には、人狼位置を知らない前提、占い師/霊媒師/騎士騙り、誤爆リスク、真役職への疑い誘導、議論撹乱などが既に含まれている。
- cross-leak tests は、狼専用語彙 (`相方`, `襲撃先を揃える`) が他役職へ漏れないこと、狂人が本物の狼位置を知っている前提にならないことを検査している。
- 既存の LLM prompt 系テストは、`tests/test_llm_prompt_builder.py` と `tests/test_llm_service.py` に集中している。

実装要求

## 1. 狂人 strategy に「吊られることの価値」を追加する

必要な仕様:
- 狂人は人狼陣営を助けるが、本物の人狼位置を知らない。
- 狂人は生存して PP/RPP に絡めるなら強いが、必ず最後まで生き残る必要はない。
- 真占い師・真霊媒師・真騎士をローラーや対抗比較に巻き込む、縄を 1 本消費させる、議論を歪める、村の進行を遅らせる、といった価値があるなら、自分が吊られるだけでも人狼陣営の仕事を果たしたことになる。
- ただし、何も巻き込まずに単独で吊られに行く、自分から破綻を急ぐ、根拠なく自吊りを要求する、といった無意味な自爆は避ける。
- 自分が吊られる価値を判断するときは、残り縄、残り人狼数の推定、PP/RPP 可能性、CO 数、真役職の信用、吊られた後に村がどこを疑うかを比較する。
- 狂人は本物の人狼位置を知らないため、狼を守るつもりの吊られ方でも誤支援や誤爆が起き得る前提で動く。

推奨文面例:
- `狂人は必ず最後まで生き残る必要はない。真役職をローラーに巻き込む、縄を 1 本使わせる、議論を歪めるなどの価値があるなら、自分が吊られるだけでも人狼陣営の仕事を果たしたことになる。`
- `ただし無意味な自吊りや単独破綻は避ける。吊られるなら、真役職・強い村位置・村の進行を巻き込み、吊られた後の盤面が人狼陣営に得になる形を選ぶ。`
- `狂人は本物の人狼位置を知らないため、吊られに行く判断でも誤爆・誤支援のリスクを見て、公開ログ上もっとも狼陣営に得な混乱を作る。`

実装方針:
- `src/wolfbot/llm/prompt_builder.py` の `_ROLE_STRATEGIES[Role.MADMAN]` に追加する。
- 既存の「本物の人狼位置を知らない」「誤爆リスク」「占い師/霊媒師/騎士騙り」の方針と矛盾しない位置へ置く。
- `相方`、`襲撃先を揃える`、人狼専用チャット、実際の狼位置を知っている前提の文面を入れない。
- common rules へは入れない。狂人専用 strategy として閉じる。

テスト:
- `tests/test_llm_prompt_builder.py`
  - `_build_strategy_block(Role.MADMAN)` に「吊られるだけでも人狼陣営の仕事を果たしたことになる」趣旨が含まれること。
  - `_build_strategy_block(Role.MADMAN)` に「無意味な自吊り」または「単独破綻」を避ける趣旨が含まれること。
  - `_build_strategy_block(Role.MADMAN)` に `相方` と `襲撃先を揃える` が含まれないこと。
- `tests/test_llm_service.py`
  - 狂人 LLM の `system_prompt` に、吊られることの価値と無意味な自爆を避ける方針が届くこと。

## 2. 人狼 strategy に「刺し違えることの価値」を追加する

必要な仕様:
- 人狼は勝利条件に必要な本体であり、基本的には生存価値が高い。
- ただし、終盤、不利盤面、相方を残せば勝ち筋がある盤面、真役職に黒を引かれそうな盤面、強い進行役に詰められている盤面では、自分の生存だけに固執しない。
- 真占い師・真霊媒師・真騎士・確白級・強い進行役・相方を疑うキープレイヤーなどを処刑、襲撃、信用勝負、黒出し、身内切り、ライン切りで 1 人落とせるなら、1 人刺し違えるだけでも人狼陣営の仕事を果たしたことになる場面がある。
- 刺し違えは勝ち筋を残すための交換であり、無計画な破綻、自分からの自白、相方を巻き込む自爆ではない。
- 刺し違える価値を判断するときは、残り縄、残り人狼数、相方の位置、PP/RPP、襲撃成功時の盤面、翌日の疑い先、CO 数、騎士護衛リスクを比較する。
- 公開発言では、実際の相方を知っている視点漏れを出さず、刺し違えの意図を露骨に語らない。

推奨文面例:
- `人狼は生存価値が高いが、終盤や不利盤面では自分の生存だけに固執しない。真役職・確白級・強い進行役・相方を疑うキープレイヤーを 1 人落とせるなら、1 人刺し違えるだけでも人狼陣営の仕事を果たしたことになる場面がある。`
- `刺し違えは勝ち筋を残すための交換であり、無計画な破綻や自白ではない。残り縄、相方の位置、PP/RPP、翌日の疑い先を見て、相方が残って勝てる形かを判断する。`
- `刺し違える動きを取る場合でも、公開発言では相方を知っている視点漏れを出さず、投票理由・騙り結果・襲撃意図がログ上自然に見える形にする。`

実装方針:
- `src/wolfbot/llm/prompt_builder.py` の `_ROLE_STRATEGIES[Role.WEREWOLF]` に追加する。
- 既存の「人狼は勝利に必要な本体であり生存価値が高い」「身内票」「ライン切り」「噛み筋」「相方との整合」の近くに置く。
- 人狼専用 strategy の外に `相方` や `刺し違え` の実行指示を漏らさない。
- common rules や村側 role strategy へは入れない。

テスト:
- `tests/test_llm_prompt_builder.py`
  - `_build_strategy_block(Role.WEREWOLF)` に「1 人刺し違えるだけでも人狼陣営の仕事を果たしたことになる」趣旨が含まれること。
  - `_build_strategy_block(Role.WEREWOLF)` に「無計画な破綻」または「自白」ではない趣旨が含まれること。
  - `_build_strategy_block(Role.VILLAGER)`, `_build_strategy_block(Role.SEER)`, `_build_strategy_block(Role.MEDIUM)`, `_build_strategy_block(Role.KNIGHT)` に、刺し違えを人狼の実行戦術として促す文面が含まれないこと。
- `tests/test_llm_service.py`
  - 人狼 LLM の `system_prompt` に、刺し違えることの価値と無計画な自爆を避ける方針が届くこと。

## 3. 既存の role separation と provider behavior を壊さない

必要な仕様:
- `Role.WEREWOLF` の内容は人狼 prompt にだけ届く。
- `Role.MADMAN` の内容は狂人 prompt にだけ届く。
- 狂人 prompt は、本物の人狼位置を知っている前提にならない。
- 人狼 prompt は、相方情報を扱えるが、公開発言で視点漏れを出さない方針を維持する。
- xAI / DeepSeek / Gemini の JSON 出力契約、`LLMAction` schema、temperature / thinking / response_format の処理は変更しない。
- `LLMAdapter._ask()` の public/private logs 読み出し、fire-and-forget dispatch、target resolver は変更しない。

テスト:
- 既存の cross-leak tests を壊さない。
- 既存の structured output tests を壊さない。
- 追加テストでは、短い anchor phrase だけでなく、役職分離と禁止語彙も確認する。

## 4. 実行するチェック

実装後、少なくとも以下を実行する:

```bash
uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py
uv run ruff check src tests
uv run mypy
```

時間や環境制約で実行できないものがある場合は、実行できなかった理由を明確に報告する。

完了条件:
- 人狼 strategy に、熟練者として「重要人物 1 人と刺し違える」交換価値を判断する方針が入っている。
- 狂人 strategy に、熟練者として「自分が吊られるだけでも仕事を果たす」縄消費・巻き込み価値を判断する方針が入っている。
- どちらも無意味な自爆を推奨しない。
- role-specific strategy の分離が保たれている。
- prompt tests と LLM service prompt delivery tests が更新されている。
- テスト・lint・型チェックの結果が報告されている。
```
