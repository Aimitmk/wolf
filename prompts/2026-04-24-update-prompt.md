# `wolfbot` 2026-04-24 更新実装プロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` を、以下の 4 点に対応するよう更新する。
- 変更はこのリポジトリの既存アーキテクチャに沿って入れ、無関係な仕様変更はしない。

今回必ず直すこと:
1. 占い師と霊媒師の判定が「人狼陣営判定」になっており、狂人が黒扱いされる。通常ルールどおり、「本物の人狼かどうかだけが分かる」挙動に直すこと。
2. 投票や夜行動で LLM プレイヤーが多いと行動完了まで遅く、ホストが長めの待ち時間を毎回設定しないと回らない。既存の進行設計を壊さず、LLM 行動の処理速度を改善すること。
3. 勝敗決定時に、参加者全員の役職をメイン公開チャンネルへ明かすこと。
4. プレイ中のゲームの LLM プレイヤーに、前回ゲームの文脈が混ざらないことをコードとテストで明確に保証すること。

最重要ルール:
- まず既存実装を読んで、現在の振る舞いとテストを把握してから直すこと。
- `domain/` は純粋ロジックのまま保ち、Discord / DB / xAI I/O は outer layer に閉じ込めること。
- 既存の optimistic lock、`WAITING_HOST_DECISION`、`/wolf extend`、`/wolf force-skip`、再起動復旧の設計を壊さないこと。
- 新しい slash command は追加しないこと。
- DB schema の変更は避けること。今回の修正は既存 schema のままで収めること。
- 天国部屋や人狼部屋の秘匿性は維持すること。ゲーム終了時の役職公開はメイン公開チャンネルだけで行い、秘密チャンネルの再利用や履歴漏洩は起こさないこと。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/domain/enums.py`
- `src/wolfbot/domain/rules.py`
- `src/wolfbot/domain/state_machine.py`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/services/game_service.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/services/discord_service.py`
- `src/wolfbot/services/permission_manager.py`
- `src/wolfbot/persistence/sqlite_repo.py`
- `tests/test_state_machine_phases.py`
- `tests/test_state_machine_nights.py`
- `tests/test_llm_service.py`
- `tests/test_game_service_advance.py`

このリポジトリで確認済みの事実:
- 現在の占い結果と霊媒結果は `FACTION_OF_ROLE` ベースで組まれており、狂人が `人狼陣営` 判定になる。
- 現在の `NIGHT_0` ランダム白は「生存中・占い師本人以外・`Role.WEREWOLF` ではない」が条件であり、狂人は合法対象に入っている。
- `GameService.advance()` 自体は LLM 投稿・投票・夜行動を fire-and-forget で起動する設計で、外側の進行 loop を直接ブロックしない。
- ただし `LLMAdapter._run_votes()` と `LLMAdapter._run_night_actions()` は各 LLM を直列ループしており、xAI round-trip が人数分積み上がる。
- 現在の `GAME_OVER` 時は勝利陣営の公開はあるが、全員の役職一覧はメイン公開チャンネルに出していない。
- 現在の LLM 文脈構築は `build_user_context()` と `SqliteRepo.load_public_logs()` / `load_private_logs_for_audience()` を通り、`game_id` 単位で DB ログを読む実装になっている。
- 現在の秘密チャンネルは `on_game_end()` で削除され、作成時にも同名 stale channel があれば削除して再利用を拒否している。

実装要求

## 1. 占い師 / 霊媒師の判定を「本物の人狼だけ黒」に直す

必要な仕様:
- 占い師と霊媒師は、対象が `Role.WEREWOLF` かどうかだけを知る。
- 狂人 (`Role.MADMAN`) は占いでも霊媒でも「人狼ではない」扱いにする。
- 役職名そのものは通知しない。
- 結果文面は陣営名ではなく、二値の人狼判定として表現すること。

このタスクで固定する仕様:
- 占い結果の文面は `占い結果: <名前> は 人狼 です。` または `占い結果: <名前> は 人狼ではありません。`
- 霊媒結果の文面は `霊媒結果: <名前> は 人狼 でした。` または `霊媒結果: <名前> は 人狼ではありませんでした。`
- `NIGHT_0` のランダム白は引き続き「本物の人狼以外」が候補なので、狂人は白候補のままでよい。
- 勝利判定のロジックは変えない。`check_victory()` や `FACTION_OF_ROLE` の勝敗用途はそのまま維持すること。

実装方針:
- 占い・霊媒専用の純粋 helper を `domain/rules.py` に追加するか、同等の責務分離で実装すること。
- `FACTION_OF_ROLE` をそのまま占い / 霊媒結果へ流用しないこと。
- `state_machine.plan_night0()` と `state_machine.plan_night_resolve()` の文面生成を更新し、既存テストも新仕様へ合わせて直すこと。

## 2. LLM の投票 / 夜行動を高速化してホスト待ちを減らす

必要な仕様:
- 既存の phase duration は原則維持し、まず LLM 側の直列処理をなくして速度改善すること。
- `GameService.advance()` の fire-and-forget 設計は維持すること。
- stale phase / stale day / ended game の再チェックと、既提出の idempotency guard は維持すること。
- `/wolf extend` 経由の再送設計も壊さないこと。

このタスクで固定する実装方針:
- `LLMAdapter._run_votes()` は各 LLM voter ごとに独立 task を起こし、並列に xAI を呼ぶ実装へ変えること。
- `LLMAdapter._run_night_actions()` も各 LLM actor ごとに独立 task を起こし、並列に xAI を呼ぶ実装へ変えること。
- 人狼チャットの事前調整ステージは「夜行動 submit 前に 1 度だけ走る」という順序を維持すること。ここは最大 2 狼のため大改造は不要だが、夜全体のボトルネックを `_run_night_actions()` に残さないこと。
- `submit_llm_daystart_speeches()` は今回の主対象ではない。昼開始の雑談テンポを大きく作り替える必要はない。

並列化時の注意:
- 各 per-seat task の直前で `repo.load_game()` を再実行し、`phase` / `day_number` / `ended_at` を確認して stale なら即 return すること。
- 投票は既存どおり `load_votes(... round_=...)` を見て、その seat の票が既にあれば skip すること。
- 夜行動は既存どおり `load_night_actions(... day=...)` を見て、その seat / kind が既にあれば skip すること。ただし split wolves の unresolved seat は再入力を許す既存条件を保つこと。
- 並列化しても例外は各 task 内で握りつぶさず、既存方針どおりログを出して他 seat の進行を止めないこと。
- 目的は「8 LLM 前後でも投票・夜行動の submit が人数ぶん直列待ちにならない」ことであり、仕様上の締切延長を常用させないこと。

## 3. GAME_OVER 時に全員の役職をメイン公開チャンネルへ出す

必要な仕様:
- 朝アナウンスや途中死亡通知では従来どおり役職を公開しない。
- ただし勝敗確定で `GAME_OVER` に入ったら、メイン公開チャンネルに最終配役を出す。
- 公開対象は参加者全員。人間 / LLM の別なく全 seat を出す。

このタスクで固定する実装方針:
- 役職公開は Discord 層の場当たり投稿ではなく、`state_machine` が生成する public log に乗せること。
- 勝利 log の直後に、追加の public log kind `ROLE_REVEAL` を出すこと。
- 文面は次の形式に固定すること。

```text
最終配役:
- 席1 Alice: 人狼 (死亡)
- 席2 Bob: 狂人 (生存)
...
```

- 生死表示は `生存` / `死亡` のみでよい。
- 勝利条件が処刑直後でも夜襲撃直後でも、両方の勝利経路で必ず `ROLE_REVEAL` が出るようにすること。

## 4. 前回ゲーム文脈が LLM に混ざらないことを保証する

必要な仕様:
- LLM が読む公開ログ・私的ログは、常に現在の `game_id` のものだけに限定する。
- Discord guild の message history を直接拾ってプロンプトに入れてはいけない。
- 終了済みゲームの private channel は再利用しない。

このタスクで固定する作業:
- 現状の `game_id` スコープの実装は維持する。
- そのうえで、前回ゲームのログが次ゲームの LLM 文脈に混ざらないことを regression test で明示的に固定する。
- 少なくとも「同じ guild・同じ seat no・同じ persona 名に見える状況でも、別 `game_id` の public/private logs は `build_user_context()` / `LLMAdapter._ask()` 側で読まれない」ことを検証する test を追加すること。
- 秘密チャンネル削除・stale channel 拒否の既存挙動は維持すること。

やってはいけないこと:
- 配役を増やす
- slash command を増やす
- Web UI を追加する
- `WAITING_HOST_DECISION` を廃止する
- 仕様確認なしでフェイズ時間を大きく変える
- 無関係なリファクタを広げる

必要なテスト変更:
- `tests/test_state_machine_phases.py`
  - `NIGHT_0` ランダム白が狂人を白扱いで含み得ることを明示する。
- `tests/test_state_machine_nights.py`
  - 占いが狂人を見ても `人狼ではありません` になる test を追加または更新する。
  - 霊媒が狂人処刑を見ても `人狼ではありませんでした` になる test を追加または更新する。
  - 既存の朝アナウンス非公開 test は維持する。
- `tests/test_llm_service.py`
  - 投票 dispatch が per-seat 直列待ちではなく並列に進むことを確認する test を追加する。
  - 夜行動 dispatch も同様に並列で進むことを確認する test を追加する。
  - 別 `game_id` のログが `_ask()` に混ざらない回帰 test を追加する。
- `tests/test_game_service_advance.py`
  - 勝利時に `ROLE_REVEAL` public log まで適用されることを確認する test を追加する。

受け入れ条件:
- 占い師 / 霊媒師は狂人を黒判定しない。
- `NIGHT_0` ランダム白の合法対象ロジックは壊れない。
- LLM の投票 / 夜行動 submit が seat 数ぶんの直列 xAI 待ちにならない。
- `GAME_OVER` 時にメイン公開ログとして全配役が出る。
- LLM 文脈が別ゲームのログを読まないことを test で保証できる。
- 既存の recovery / resend / force-skip 挙動を壊さない。

実行する検証コマンド:
- `uv run pytest tests`
- `uv run ruff check src tests`
- `uv run mypy`

最後に簡潔に報告すること:
- 何を変えたか
- どのファイルを変えたか
- 実行した検証コマンドと結果
- 残課題があればその内容
```
