# `wolfbot` 2026-04-25 LLM Werewolf Guard-Aware Attack Prompt

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player が人狼として夜の襲撃先を決めるときに、熟練した人狼プレイヤーのように「騎士がどこを守りそうか」「誰が騎士っぽいか」「GJ リスクと襲撃価値の釣り合い」を考慮できるようにすることです。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player を、人狼役の夜襲撃判断においてより強い熟練者として振る舞わせる。
- 人狼 LLM が襲撃先を選ぶとき、単に怪しい相手や情報役を噛むだけでなく、騎士の護衛先予測、騎士候補推定、GJ リスク、襲撃価値、相方との合意を明示的に比較するよう prompt を強化する。
- ただし実際の騎士役職、護衛先、夜行動など、本人に見えていない秘匿情報を漏らしてはならない。

今回必ず対応すること:
1. 人狼 role-specific strategy に、護衛読み・騎士候補推定・GJ リスク評価・襲撃価値比較を追加すること。
2. 人狼の夜行動 task (`WOLF_ATTACK`) に、候補ごとに「襲撃価値」「護衛されやすさ」「騎士候補度」を考えてから選ぶよう明記すること。
3. 人狼チャット task に、相方と襲撃候補を相談するときも「なぜその候補が噛みたいか」「護衛リスクはどの程度か」「騎士探しになるか」を短く共有させること。
4. 非人狼 role の system prompt に、人狼専用の襲撃戦術や相方連携語彙を漏らさないこと。
5. DB schema、Discord command、ゲームルール、状態遷移、夜処理の解決ロジックは変更しないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築と LLM 夜行動フローを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つ。今回の変更は `llm/` と必要最小限の `services/llm_service.py` テスト補強に閉じること。
- Discord channel history を直接読んで prompt に入れてはいけない。LLM 文脈は既存どおり DB の public/private logs からだけ構築すること。
- `game_id` / `audience_seat` ベースの情報分離を壊さないこと。
- `Player.role` から実役職を推定表として非公開 seat に漏らしてはいけない。
- 人狼 LLM には、自分の役職として見えている人狼仲間と人狼チャットだけを使わせる。狂人位置や騎士位置を知っている前提にしない。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/domain/enums.py`
- `src/wolfbot/domain/rules.py`
- `src/wolfbot/domain/state_machine.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`
- `tests/test_rules_night_targets.py`

このリポジトリで確認済みの事実:
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md`。
- system prompt は `src/wolfbot/llm/prompt_builder.py::build_system_prompt()` が合成する。
- user context は `src/wolfbot/llm/prompt_builder.py::build_user_context()` が作る。
- 人狼 role-specific strategy は `src/wolfbot/llm/prompt_builder.py` の `_ROLE_STRATEGIES[Role.WEREWOLF]` にある。
- 夜行動 task は `task_night_action(kind, candidate_tokens)` が作る。
- 人狼チャット task は `task_wolf_chat(partner_tokens, candidate_tokens)` が作る。
- `LLMAdapter._run_wolf_chat()` は夜行動提出前に LLM 人狼を順に発言させるため、後続の LLM 人狼は先に投稿された人狼チャットを private log として読める。
- `LLMAdapter._ask()` は `repo.load_public_logs(game.id, limit=40)` と `repo.load_private_logs_for_audience(game.id, audience_seat=player.seat_no, limit=40)` を使う。
- 人狼相方情報は `build_user_context()` 内で `me.role is Role.WEREWOLF` の場合だけ user context に入る。
- 既存 prompt には、噛み筋、狩人探し、騎士の連続護衛不可、騎士 CO、偽騎士日記などの語彙はある。
- ただし現状の人狼向け夜襲撃 task は、候補選択時に「騎士が守りそうな先」「誰が騎士っぽいか」「護衛成功リスクと襲撃価値」を比較するようには十分に明示していない。

実装方針:
- まずは prompt 強化で対応する。新しい DB カラム、夜行動ルール、候補生成ロジック、Discord UI は追加しない。
- `build_system_prompt()` の公開引数は変えない。
- `build_user_context()` の秘匿範囲は変えない。
- `task_night_action()` と `task_wolf_chat()` の戻り文面を強化する。
- 必要なら `prompt_builder.py` 内に人狼襲撃判断用の小さな helper 文字列を追加してよい。
- コード上は人狼にだけ差し込まれる文面にする。非狼 role の prompt に人狼専用戦術を入れない。

## 1. 人狼 strategy に護衛読みと騎士候補推定を追加する

`_ROLE_STRATEGIES[Role.WEREWOLF]` に、以下の趣旨を必ず追加すること。

必須内容:
- 夜の襲撃先は、候補ごとに `襲撃価値 / 護衛されやすさ / 騎士候補度 / 相方との整合` を比較して選ぶ。
- 護衛されやすい位置は、単独真寄りの占い師・霊媒師、確白寄り、直近で白をもらった重要位置、村の進行役、強く信頼されている発言者である。
- 護衛されやすい位置は襲撃価値も高いことが多いが、GJ で縄が増えたり、噛み失敗で人狼側が不利になったりするため、毎回無条件に噛まない。
- 騎士候補は、騎士 CO、護衛先を匂わせる発言、情報役を守りたがる発言、平和な朝の反応、処刑回避の仕方、発言量の抑え方などから公開情報ベースで推定する。
- 騎士でなさそうな位置も同時に考える。騎士 CO を強く促しすぎる位置、護衛ルールを誤っている位置、死を恐れない視点の位置などは騎士候補から下げる。ただし断定しない。
- 襲撃方針は、情報役噛み、白位置噛み、意見噛み、騎士探し、SG を残す噛みのどれに当たるかを整理する。
- 騎士候補を噛む場合は、短期的な情報役噛みよりも、翌日以降に安全に情報役を噛む準備として価値がある。
- 護衛濃厚な真役職を噛みに行く場合は、GJ リスクを承知で勝負する理由を持つ。例えばその役職を残すと黒を引かれる、霊媒結果で破綻する、進行役として村を固められるなど。
- 相方の投票・発言・騙り結果と噛み筋が矛盾しないようにする。襲撃先が相方を不自然に白くしすぎたり、露骨な意見噛みに見えたりするリスクも見る。
- 最終的には人狼チャットで相方と 1 人に揃える。自分の第一希望だけでなく、相方案がある場合は護衛リスクと襲撃価値を比較して合わせる。

文面例:

```text
- 夜の襲撃先は、候補ごとに「襲撃価値」「護衛されやすさ」「騎士候補度」「相方との整合」を比較して選ぶ。単独真寄りの情報役や確白寄りは価値が高い一方、騎士護衛も集まりやすい。
- 騎士候補は公開ログから推定する。騎士 CO、護衛先を匂わせる発言、情報役を守りたがる姿勢、平和な朝の反応、処刑回避の仕方を材料にする。ただし実役職を知っている前提で断言しない。
- 噛み方針を、情報役噛み・白位置噛み・意見噛み・騎士探し・SG 残しのどれかとして整理し、翌日の主張や相方の位置と矛盾しない襲撃を選ぶ。
```

注意:
- `Role.WEREWOLF` 以外の strategy に `相方`、`襲撃先を揃える`、`騎士候補を噛む`、`護衛リスクを読んで噛む` のような人狼専用語彙を入れないこと。
- 共通ルールには一般語彙として `噛み筋` や `狩人探し` があってよいが、相方連携や襲撃最適化の tactical advice は人狼 strategy に置くこと。

## 2. `task_night_action(WOLF_ATTACK)` を強化する

`task_night_action(kind, candidate_tokens)` で `kind is SubmissionType.WOLF_ATTACK` の場合、現在の「相方案に合わせる」指示に加えて、襲撃判断のチェックリストを入れること。

必須内容:
- 候補トークンから 1 名を厳密に選ぶルールは維持する。
- 人狼の襲撃対象を選ぶ前に、候補ごとに以下を短く比較するよう指示する。
  - `襲撃価値`: 情報役、確白寄り、進行役、強い村目、相方を疑っている位置か。
  - `護衛されやすさ`: 騎士が守りそうな位置か。単独真寄り情報役、白位置、進行役は護衛されやすい。
  - `騎士候補度`: その本人が騎士っぽいか。騎士探しとして噛む価値があるか。
  - `翌日の説明`: その噛み筋が自分や相方の発言・投票・騙り結果と矛盾しないか。
- 相方が人狼チャットで案を出している場合は、強い反対理由がなければ合わせる。反対する場合も最終的に 1 人へ揃える必要があることを維持する。

文面例:

```text
 人狼の襲撃では、候補ごとに「襲撃価値」「護衛されやすさ」「騎士候補度」「翌日の説明しやすさ」を比較してください。単独真寄りの情報役・確白寄り・進行役は価値が高い一方で護衛も集まりやすいです。騎士っぽい相手を先に噛む選択も検討してください。
```

## 3. `task_wolf_chat()` を強化する

`task_wolf_chat(partner_tokens, candidate_tokens)` の人狼チャット指示を、単なる候補提示ではなく、熟練者の短い相談にする。

必須内容:
- `public_message` は人狼専用チャットへの投稿だが、既存設計上フィールド名は `public_message` のままでよい。
- 80〜150 字の目安は維持する。
- 1 名の襲撃候補と理由を出すだけでなく、以下のうち重要なものを 1〜2 点含めるよう指示する。
  - 噛みたい理由 (情報役 / 白位置 / 意見噛み / 騎士探し / SG 残し)
  - 護衛されそうか
  - 本人が騎士っぽいか
  - 相方の案へ賛成/反対する理由
- 相方が既に案を出している場合は、護衛リスクと襲撃価値を比較したうえで、最終的に 1 人へ揃えることを優先させる。

文面例:

```text
 `intent=speak` と `public_message` に、1 名の襲撃候補と理由を 80〜150 字で書いてください。理由には、襲撃価値、護衛されそうか、騎士候補として噛む価値があるか、相方案への賛否のうち重要な 1〜2 点を含めてください。
```

## 4. 情報秘匿と role leak を壊さない

必要な仕様:
- 非人狼 prompt に、人狼相方情報や人狼専用襲撃戦術が混ざってはいけない。
- 騎士候補推定は公開ログと人狼本人に見えている private log からの推理であり、DB 上の実役職を渡してはいけない。
- `build_user_context()` の `wolf_partner_block` は既存どおり `me.role is Role.WEREWOLF` の場合だけ表示する。
- `task_night_action(WOLF_ATTACK)` は人狼夜行動の task としてだけ使われる前提だが、テストで文言の分岐を固定する。
- 共通ルールに人狼専用の tactical advice を増やしすぎない。全役職に見える system prompt の共通ブロックは、公開ルールと一般的な推理語彙に留める。

やってはいけないこと:
- 騎士の実 seat を `Player.role` から人狼 prompt に渡す。
- 騎士の前回護衛先や今回護衛先を、人狼に見える情報として渡す。
- 夜行動解決順や護衛成功判定を変更する。
- 襲撃候補から護衛濃厚位置を自動除外する。
- LLM の `target_name` を候補外でも通す。
- 人狼以外の strategy に `相方`、`襲撃先を揃える`、`騎士候補を噛む` などを入れる。
- DB schema、slash command、Discord UI を変更する。

## 5. テストを追加 / 更新する

`tests/test_llm_prompt_builder.py`:
- `_build_strategy_block(Role.WEREWOLF)` に `襲撃価値`、`護衛されやすさ`、`騎士候補度`、`GJ` または `護衛リスク` が含まれること。
- `_build_strategy_block(Role.WEREWOLF)` に、情報役噛み・白位置噛み・意見噛み・騎士探し・SG 残しの語彙が含まれること。
- `_build_strategy_block(Role.WEREWOLF)` に、相方と 1 人に揃える指示が維持されること。
- `Role.MADMAN`、`Role.SEER`、`Role.MEDIUM`、`Role.KNIGHT`、`Role.VILLAGER` の strategy に、人狼専用の `相方`、`襲撃先を揃える`、`騎士候補を噛む`、`護衛リスクを読んで噛む` が漏れないこと。
- `task_night_action(SubmissionType.WOLF_ATTACK, ...)` に、襲撃価値・護衛されやすさ・騎士候補度・翌日の説明しやすさの指示が含まれること。
- `task_night_action(SubmissionType.SEER_DIVINE, ...)` と `task_night_action(SubmissionType.KNIGHT_GUARD, ...)` には、人狼専用の襲撃チェックリストが含まれないこと。
- `task_wolf_chat(...)` に、護衛リスク、騎士候補、相方案への賛否、1 人に揃える指示が含まれること。

`tests/test_llm_service.py`:
- `_CapturingDecider` を使い、LLM 人狼の夜行動 `_ask()` に渡る `system_prompt` に新しい人狼 strategy が含まれることを検証する。
- `_CapturingDecider` を使い、LLM 人狼の `task_text` 経由で `system_prompt` に入る WOLF_ATTACK 指示に、襲撃価値・護衛されやすさ・騎士候補度が含まれることを検証する。
- 非狼 LLM の `system_prompt` に、人狼専用の襲撃チェックリストが入らないことを検証する。
- 既存の `game_id` / `audience_seat` スコープ分離のテストを壊さないこと。

既存テスト群は壊さないこと:
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`
- `tests/test_rules_night_targets.py`

## 6. 受け入れ条件

- 人狼 LLM は夜の襲撃候補を、襲撃価値・護衛されやすさ・騎士候補度・翌日の説明しやすさで比較する prompt を受け取る。
- 人狼チャットでは、相方と襲撃先を揃えつつ、護衛リスクや騎士探しの意図を短く共有できる。
- 非人狼 LLM に人狼専用の襲撃戦術や相方連携語彙が漏れない。
- 騎士の実役職や護衛先は人狼に漏れない。あくまで公開情報からの推定として扱われる。
- DB schema、状態遷移、夜処理解決、Discord command、UI は変わらない。
- mypy strict、ruff、関連 pytest が通る。

## 7. 検証コマンド

最低限:

```bash
uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py
uv run ruff check src tests
uv run mypy
```

可能なら関連範囲も走らせる:

```bash
uv run pytest tests/test_llm_structured_output.py tests/test_llm_resolver.py tests/test_llm_trigger.py tests/test_rules_night_targets.py
```

最後に簡潔に報告すること:
- どの prompt 文面を強化したか
- 人狼専用情報が非狼 prompt に漏れないようにした境界
- 実行したテスト / lint / 型チェックと結果
- 残課題があればその内容
```
