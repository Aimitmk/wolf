# 人狼bot コードレビュー結果

## 概要

- 対象: `src/wolfbot`, `tests`, `prompts/IMPLEMENTATION_PROMPT.md`
- 観点: ゲーム進行、復旧、秘密情報の隔離、Discord UI の有効期限、LLM/非同期タスクの運用安全性
- 結論: テスト・静的解析は通過している一方で、実運用では高優先度の問題が 2 件、中優先度の問題が 1 件あります

## 実施した確認

- `uv run pytest tests -q` → `146 passed, 1 warning in 0.80s`
- `uv run ruff check src tests` → `All checks passed!`
- `uv run mypy` → `Success: no issues found in 25 source files`
- 主要モジュールの静的確認:
  - `domain/rules.py`, `domain/state_machine.py`
  - `services/game_service.py`, `services/discord_service.py`, `services/recovery_service.py`, `services/llm_service.py`, `services/permission_manager.py`
  - `persistence/sqlite_repo.py`, `persistence/schema.py`
  - `ui/views.py`

## Findings

### High: 秘匿チャンネルを再利用しており、前村の人狼チャット/天国チャット履歴が次村に漏れる

- 影響:
  - 新しいゲームの参加者が、前のゲームで使われた `wolf-wolves` / `wolf-heaven` の過去ログを閲覧できます。
  - 人狼陣営の夜会話や死者会話は次ゲームの参加者に見せてはいけない情報で、ゲーム間の秘密保持が崩れます。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:340`
  - `src/wolfbot/services/discord_service.py:639`
  - `src/wolfbot/services/permission_manager.py:120`
- 詳細:
  - `/wolf create` は毎回 `_create_private_channel(...)` を呼びますが、同名チャンネルがすでに存在すると新規作成せずそのまま返します。
  - 一方でゲーム終了時の `PermissionManager.on_game_end()` はメンバー overwrite を外すだけで、チャンネル削除も履歴削除もしていません。
  - そのため、次ゲームで同じチャンネル ID を再利用し、新しい死者や新しい人狼に閲覧権限を付け直すと、過去ゲームの秘密ログまで見えてしまいます。
- 発生シナリオ:
  - ゲーム A 終了後、`wolf-wolves` / `wolf-heaven` を残したままゲーム B を作る。
  - ゲーム B の人狼または死亡者がそのチャンネルを開くと、ゲーム A の発言履歴を遡って読める。
- 改善案:
  - 秘匿チャンネルはゲームごとに新規作成し、終了時に削除するのが安全です。
  - 再利用を続けるなら、少なくとも開始前に履歴を完全に隔離できる設計に変える必要があります。

### High: bot 再起動後、投票/夜行動 DM の UI が復旧されず、人間プレイヤーが提出できなくなる

- 影響:
  - `DAY_VOTE` / `DAY_RUNOFF` / `NIGHT` の途中で bot が再起動すると、復旧後にゲーム phase は継続していても、人間プレイヤーの DM UI は実質使えません。
  - その結果、未提出者は自力で投票や夜行動を再送できず、ホストの `/wolf extend` や `/wolf force-skip` に依存する壊れた運用になります。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:128`
  - `src/wolfbot/services/discord_service.py:158`
  - `src/wolfbot/ui/views.py:21`
  - `src/wolfbot/ui/views.py:72`
  - `src/wolfbot/main.py:41`
  - `src/wolfbot/services/recovery_service.py:93`
- 詳細:
  - 投票 DM と夜行動 DM は、その場で生成した `VoteView` / `NightActionView` のインメモリ callback に依存しています。
  - 起動時には既存 DM メッセージ用の view 再登録がなく、recovery 側も `reconcile` と `announce_recovery` と engine attach しかしていません。未提出者への DM 再送もありません。
  - この組み合わせでは、再起動前に送った DM メッセージの component は process restart 後に復旧されません。
- 発生シナリオ:
  - `DAY_VOTE` の締切前に bot を再起動する。
  - recovery は phase を維持して engine を付け直すが、プレイヤーが既存 DM を押しても提出経路が復元されない。
  - `WAITING_HOST_DECISION` から `/wolf extend` で再開しても、古い DM UI を再送していないため同じ問題が残ります。
- 改善案:
  - restart 後も機能する persistent view を設計して `bot.add_view(...)` で復元するか、recovery / extend 時に未提出者へ新しい DM UI を再送する必要があります。
  - いずれの方針でも、古い UI と新しい UI の整合性を管理する仕組みが必要です。

### Medium: 古い DM UI からの無効な提出でも、ユーザーには「受け付けました」と表示される

- 影響:
  - 実際には保存されていない投票や夜行動が、ユーザーには成功したように見えます。
  - とくに決選投票移行後や翌フェイズ移行後に古い DM を押すと、提出漏れに気づけず `WAITING_HOST_DECISION` に入る原因になります。
- 根拠コード:
  - `src/wolfbot/ui/views.py:50`
  - `src/wolfbot/ui/views.py:105`
  - `src/wolfbot/services/game_service.py:366`
  - `src/wolfbot/services/game_service.py:422`
- 詳細:
  - `VoteView` / `NightActionView` は callback が例外なく戻れば必ず「受け付けました」と返します。
  - しかし `GameService.submit_vote()` / `submit_night_action()` は、stale phase・死者・違法 target などを例外ではなく `log.info(...); return` で握りつぶします。
  - そのため、「保存失敗ではないが受理もされていない」ケースを UI が成功扱いしてしまいます。
- 発生シナリオ:
  - ユーザーが 1 回目投票の古い DM を、すでに `DAY_RUNOFF` に入ったあとで押す。
  - service 層は stale vote として無視する。
  - それでも DM 画面は「投票を受け付けました。」に更新され、ユーザーは未投票に気づけない。
- 改善案:
  - submission callback の返り値を `bool` や `enum` にして、`accepted / stale / invalid` を UI に返すべきです。
  - フェイズ遷移時に古い DM を disable するか、新しい DM を送った時点で古い UI を使えない扱いにする設計も必要です。

## Open Questions / Residual Risks

- `LLMAdapter.submit_llm_daystart_speeches()` は fire-and-forget の background task を作りますが、`_run_daystart()` / `_maybe_speak()` では投稿前に最新 phase を再確認していません。議論終了後や復旧後に stale な昼発言が混ざらないか、追加検証した方が安全です。
- 現在のテストは通常遷移と復旧の骨格をよく押さえていますが、以下の運用系異常は未カバーです。
  - 再起動後の DM component 復旧
  - ゲームまたぎの秘密チャンネル履歴隔離
  - stale DM 操作時のユーザー向け応答

## 短い総括

通常フローの状態遷移、永続化、復旧基盤はかなり整理されています。現時点で優先度が高いのは、ゲーム間の秘密情報漏えいを防ぐことと、再起動後も人間プレイヤーの提出経路を壊さないことです。この 2 点を直さない限り、実運用では bot 再起動や複数回開催だけでゲーム体験が破綻します。
