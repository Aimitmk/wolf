# `wolfbot` 2026-04-25 LLM 熟練者化・村人CO抑止・day3投票待機化修正プロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player を強い熟練した人狼プレイヤーとして振る舞わせることです。特に、村人が「普通の村人」として不用意に CO する癖を止め、過去に対抗 CO がいた役職を現在 1 人だけ残っているという理由だけで真置きしないようにし、day3 の投票フェイズが開始直後にホスト待ちへ落ちる進行バグを直します。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player を、より強い熟練した人狼プレイヤーとして振る舞わせる。
- LLM の村人が「村人CO」「素村CO」「普通の村人です」のような、弱く不自然な自己 CO をしないようにする。
- 過去に同じ役職 CO が 2 人以上出ていた場合、処刑・襲撃で現在 1 人だけ残っても、その残存 CO 者を自動的に真置きしない方針をさらに強める。
- day3 の投票フェイズが、投票開始直後に未投票者ありとして `WAITING_HOST_DECISION` へ入る不具合を再現テスト付きで修正する。

今回必ず対応すること:
1. LLM 村人は、能動的に「村人CO」「素村CO」「普通の村人です」「役職は村人です」と名乗らない。
2. LLM 村人は、自分に私的情報がないことを理由に、CO ではなく「非 CO の灰」として公開情報から推理する。
3. 特定役職 CO が過去に 2 人以上いた場合は、現在の生存 CO 者が 1 人になっても「対抗なし単独 CO」として扱わない方針を system prompt 上で強く明文化する。
4. day3 の `DAY_VOTE` / `DAY_RUNOFF` は、投票開始直後や stale wake で `GameService.advance()` が呼ばれても、deadline 前かつ未完了なら同じ phase のまま待つ。
5. 投票 deadline 到達後に未投票者がいる場合は、従来どおり `WAITING_HOST_DECISION` に入り、ホストが `/wolf extend` または `/wolf force-skip` で処理する。
6. 全生存者の投票が揃った場合は、deadline 前でも従来どおり早期 resolve してよい。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築、LLM fire-and-forget、GameEngine wake、投票 phase resolve の流れを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つこと。Discord I/O、LLM I/O、DB I/O を domain に入れない。
- 投票の「deadline 前は待つ」判断は、原則として `GameService._plan_next()` など orchestration 側で行うこと。`plan_day_vote_resolve()` / `plan_day_runoff_resolve()` の domain 関数は deadline を知らないため、無理に時刻依存を入れない。
- 既存の optimistic lock (`SqliteRepo.apply_transition(..., expected_phase=...)`) を迂回しないこと。
- LLM の xAI 呼び出しを `GameService.advance()` の通常パスで長時間 await しないこと。
- Discord channel history を直接 prompt に入れないこと。LLM 文脈は既存どおり DB の public/private logs から構築すること。
- user context に新しい CO parser や機械集計ブロックを追加しない。今回は prompt 文面と進行バグ修正に絞る。
- slash command、配役、勝利条件、DB schema は今回の目的に不要なら変更しないこと。
- 無関係な大規模 refactor をしないこと。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/services/game_service.py`
- `src/wolfbot/services/timer_service.py`
- `src/wolfbot/services/recovery_service.py`
- `src/wolfbot/domain/enums.py`
- `src/wolfbot/domain/state_machine.py`
- `src/wolfbot/domain/rules.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_trigger.py`
- `tests/test_game_service_advance.py`
- `tests/test_state_machine_votes.py`
- `tests/fakes.py`

このリポジトリで確認済みの事実:
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md` で、実際の共通ルール・役職別 strategy は `src/wolfbot/llm/prompt_builder.py` が組み立てている。
- `_build_game_rules_block()` には、すでに「対抗 CO が一度もない単独 CO は真寄り」「過去に同じ役職 CO が 2 人以上いた場合、現在 1 人だけ残っても自動的に真置きしない」趣旨の文面がある。
- `_ROLE_STRATEGIES[Role.VILLAGER]` には、すでに「占い/霊媒/騎士の CO 騙りは村陣営としては行わない」とあるが、「村人CO」「素村CO」「普通の村人です」をしないことは明示が弱い。
- `tests/test_llm_prompt_builder.py` と `tests/test_llm_service.py` には、共通 CO 評価方針や role-specific strategy の prompt 到達を確認するテストがある。
- `GameEngine` は `deadline_epoch` または `wake()` で `GameService.advance(game_id)` を呼ぶ。
- `GameService._plan_next()` は現在 phase ごとに domain transition を選び、`DAY_VOTE` / `DAY_RUNOFF` では votes を読んで `plan_day_vote_resolve()` / `plan_day_runoff_resolve()` を呼ぶ。
- `plan_day_vote_resolve()` / `plan_day_runoff_resolve()` は、未投票者がいて `force_skip=False` なら `WAITING_HOST_DECISION` へ遷移する。
- したがって、投票 deadline 前に stale wake や予期しない `advance()` が `DAY_VOTE` / `DAY_RUNOFF` で走ると、未投票者がいるだけで即ホスト待ちになるリスクがある。
- 投票 UI / LLM 投票が正しく動いていても、day3 のような後半日に古い wake、recovery、手動 advance、非同期 task 完了が重なるとこの症状が見える可能性がある。

実装要求

## 1. LLM 村人の「村人CO」「素村CO」を禁止する

必要な仕様:
- LLM 村人は、昼発言で自分を「村人CO」「素村CO」「普通の村人です」「役職は村人です」と名乗らない。
- 村人は能力結果を持たないため、自分の白や村陣営を CO で証明しようとしない。
- 役職 CO をしていない立場を説明する必要がある場合は、「現時点で役職 CO はしない」「非 CO の灰として見る」「CO ではなく発言・投票・判定で詰める」のように言い換える。
- ただし、村人が占い師・霊媒師・騎士を騙らない既存ルールは維持する。
- 人狼・狂人が必要に応じて偽 CO する既存 strategy は壊さない。
- 真占い師・真霊媒師・真騎士が適切な場面で CO する既存 strategy は壊さない。

実装方針:
- `src/wolfbot/llm/prompt_builder.py` の `_ROLE_STRATEGIES[Role.VILLAGER]` を更新する。
- 既存の村人 strategy の近くに、以下の趣旨を明文化する。
  - `村人は「村人CO」「素村CO」「普通の村人です」と名乗って信用を取ろうとしない。熟練者として、非 CO の灰なら公開情報から推理し、CO ではなく発言・投票・判定履歴で白さを取る。`
  - `聞かれた場合も「役職 CO はない」「非 CO」と答えるに留め、「村人役職をCOする」形にはしない。`
- system template の大構造は変えない。必要なら共通ルールにも短い補足を入れてよいが、基本は villager strategy に閉じる。
- LLM の structured output schema は変えない。
- user context に新しい情報を追加しない。

テスト:
- `tests/test_llm_prompt_builder.py`
  - `_build_strategy_block(Role.VILLAGER)` に「村人CO」「素村CO」をしない趣旨が含まれること。
  - 既存の「CO 騙りは村陣営としては行わない」趣旨も残ること。
  - 狼・狂人 strategy にこの村人専用禁止文が不要に混ざらないこと。
- `tests/test_llm_service.py`
  - `_ask()` が組み立てた villager の `system_prompt` に、村人CO/素村CO禁止方針が届くこと。
  - seer / medium / knight の prompt では、真役職としての CO 方針が消えていないこと。

## 2. 対抗 CO 履歴ありの残存 1 CO を自動真置きしない方針をさらに強める

必要な仕様:
- 「現在生存している CO 者が 1 人だけ」と「公開ログ上、その役職への対抗 CO が一度も出ていない」は別物として扱う。
- 同じ役職 CO が過去に 2 人以上存在した場合、対抗者が処刑・襲撃・突然死・その他の理由で死亡していても、残存 CO 者を対抗なし単独 CO として真置きしない。
- 対抗 CO 履歴がある役職では、死亡済み CO 者も推理対象に残し、以下を比較する。
  - CO タイミング
  - 判定結果の整合性
  - 発言時系列
  - 投票履歴
  - 襲撃結果
  - 死亡タイミング
  - 霊媒結果
- 「最後まで残ったから真」ではなく、「なぜ噛まれず残っているか」「狼が残したい CO ではないか」「吊られた対抗の色や発言はどうだったか」を見る。

実装方針:
- `src/wolfbot/llm/prompt_builder.py` の `_build_game_rules_block()` にある既存文面を、より誤解しにくい表現へ補強する。
- 既存の「対抗 CO が一度もない単独 CO は真寄り」は残す。これは今回削らない。
- 補強するのは、「過去に対抗 CO がいた場合は、現在 1 人でも単独 CO 扱いしない」という境界条件。
- CO parser、盤面分類、自動役職推定、DB schema は追加しない。
- role-specific strategy に同じ長文を重複させず、共通ルールとしてすべての LLM seat に届くようにする。

推奨文面例:
- `「現在 1 人だけ残っている CO」と「最初から対抗が一度も出ていない単独 CO」は別物。過去に同じ役職 CO が 2 人以上いたなら、現在の残存 CO 者を対抗なし単独 CO として真置きしない。`
- `対抗 CO 履歴がある役職では、死亡済み CO 者も候補から消さず、CO タイミング・判定結果・発言時系列・投票・襲撃・死亡タイミング・霊媒結果を比較する。`
- `最後まで生き残った CO 者は真とは限らない。狼が噛まずに残した、または対抗を吊らせて信用を取った可能性も見る。`

テスト:
- `tests/test_llm_prompt_builder.py`
  - `_build_game_rules_block()` に「現在 1 人だけ残っている CO」と「対抗なし単独 CO」を区別する文面があること。
  - 「最後まで残ったから真ではない」趣旨が含まれること。
  - 死亡済み CO 者を推理対象として保持する趣旨が含まれること。
- `tests/test_llm_service.py`
  - 任意の role の `system_prompt` に、補強後の共通 CO 評価方針が入ること。

## 3. day3 投票が即ホスト待ちになる進行バグを直す

必要な仕様:
- `DAY_VOTE` / `DAY_RUNOFF` に入った直後、deadline 前で未投票者がいるだけでは `WAITING_HOST_DECISION` に遷移しない。
- `DAY_VOTE` / `DAY_RUNOFF` の resolve 条件は以下のいずれか:
  - 全生存者の投票が揃った
  - `now >= game.deadline_epoch`
  - host force-skip により `game.force_skip_pending=True`
- deadline 前かつ未完了なら、`GameService.advance()` が呼ばれても transition を返さず、その phase のまま待つ。
- day1/day2/day3 以降で同じ挙動にする。day3 だけ特別扱いしない。
- deadline 到達後に未投票者がいる場合は、従来どおり `WAITING_HOST_DECISION` に入り、pending vote を作る。
- 全投票が揃った場合は、deadline 前でも従来どおり即 resolve し、処刑 / 決選 / 夜へ進める。
- `/wolf force-skip` 後は、未投票者を棄権扱いにして resolve する既存挙動を維持する。

推奨実装方針:
- `GameService._plan_next()` の `Phase.DAY_VOTE` 分岐で、`plan_day_vote_resolve()` を呼ぶ前に以下を判定する。
  - `deadline_passed = game.deadline_epoch is not None and now >= game.deadline_epoch`
  - `all_votes_in = alive_seats <= submitted_voter_seats`
  - `if not deadline_passed and not all_votes_in and not game.force_skip_pending: return None`
- `Phase.DAY_RUNOFF` 分岐にも同じ guard を入れる。round は 1、alive voters は生存者、候補は tied candidates のまま。
- guard は domain 層ではなく service 層に置く。domain の `plan_day_vote_resolve()` / `plan_day_runoff_resolve()` は、呼ばれた時点で resolve すべきだという前提を保つ。
- 可能なら、`GameService` 内に小さな private helper を作り、通常投票と決選投票で重複を減らす。
- `GameEngine` の wake clear は既存のまま尊重する。今回の修正は、たとえ stale wake が残っても service 側で早期 WAITING にしない防御として入れる。
- LLM vote fire-and-forget は維持する。LLM 投票が一部だけ先に提出されても、人間の投票 deadline 前なら host waiting にしない。
- `RecoveryService` の「期限切れ submission phase は WAITING にする」仕様は維持する。ただし deadline 前の active `DAY_VOTE` / `DAY_RUNOFF` は recovery 後も engine attach + DM resend に留める。

テスト:
- `tests/test_game_service_advance.py`
  - `DAY_VOTE` day 3 を作り、deadline 未来・未投票者ありの状態で `service.advance(game.id)` を呼んでも `Phase.DAY_VOTE` のままであること。
  - 同じ状態で deadline 到達後に `service.advance(game.id)` を呼ぶと `WAITING_HOST_DECISION` になること。
  - day 3 の `DAY_VOTE` で全生存者が投票済みなら、deadline 前でも resolve すること。
  - `DAY_RUNOFF` でも deadline 前・未完了では待ち、全投票済みまたは deadline 後にのみ resolve すること。
  - LLM vote dispatch 後、LLM だけが投票済みで人間が未投票の状態でも、deadline 前の `advance()` で host waiting に落ちないこと。
- `tests/test_state_machine_votes.py`
  - domain 関数単体では既存どおり、未投票者ありで呼ばれたら `WAITING_HOST_DECISION` を返すテストを維持する。今回の deadline guard は service 層の責務であることをテスト構成で明確にする。
- `tests/test_recovery.py`
  - 既存の deadline 期限切れ recovery が `WAITING_HOST_DECISION` へ入る挙動を壊さないこと。
  - deadline 前の `DAY_VOTE` recovery が即 WAITING にならない既存/追加テストを必要に応じて補強する。

## 4. 受け入れ条件

- LLM 村人の system prompt に、村人CO/素村COをしない方針が明示されている。
- LLM 村人は、村人であることを CO として信用材料にせず、公開ログ・CO 履歴・判定履歴・投票履歴・噛み筋・縄数から推理するよう促される。
- 対抗 CO が一度もない単独 CO を真寄りに扱う既存方針は残る。
- 過去に同じ役職 CO が 2 人以上いた場合、現在 1 人だけ残っても自動的に真置きしない方針が、すべての LLM player の system prompt に入る。
- day3 の投票フェイズ開始直後に `GameService.advance()` が呼ばれても、deadline 前・未投票ありなら `DAY_VOTE` のまま待つ。
- 投票 deadline 後に未投票者がいれば、従来どおり `WAITING_HOST_DECISION` に入る。
- 全投票が揃った場合は、deadline 前でも投票結果を resolve する。
- day1/day2/day3+ の投票挙動が一貫している。
- LLM fire-and-forget、recovery、optimistic lock、DM resend の既存設計を壊していない。

実行する検証コマンド:
- `uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py tests/test_game_service_advance.py tests/test_state_machine_votes.py tests/test_recovery.py`
- `uv run ruff check src tests`
- `uv run mypy`

最後に簡潔に報告すること:
- 何を変えたか
- どのファイルを変えたか
- 追加したテスト
- 実行した検証コマンドと結果
- 残課題があればその内容
```
