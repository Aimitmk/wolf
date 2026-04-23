# 人狼bot コードレビュー結果

## 総評

`uv run pytest tests -q`、`uv run mypy`、`uv run ruff check src tests` はすべて通過しており、状態遷移・永続化・Recovery の基本品質は高いです。一方で、実運用で効く権限制御と起動時復旧まわりに、仕様逸脱または復旧不能につながる問題が 3 件残っています。

インターフェース変更: なし

## Findings

### High: 非参加者のメイン text 発言を bot が防いでおらず、その投稿が LLM 反応入力にも入る

Impact:
既存メインチャンネルの `@everyone` に送信権限が残っている構成では、参加者以外や観戦者も昼フェイズ中に発言できます。その投稿は `WolfCog.on_message()` 経由で LLM の反応トリガにも入るため、ゲーム外ユーザーが議論を汚染したり LLM の発話を誘導したりできます。

Evidence:
`prompts/IMPLEMENTATION_PROMPT.md:134-135` は「昼は生存者だけがメイン text に発言可能」「死亡者はメイン text を read-only」と要求しています。実装側では `src/wolfbot/services/permission_manager.py:62-64` と `src/wolfbot/services/permission_manager.py:160-181` が着席済みメンバーにだけ個別上書きを当てており、非参加者や `@everyone` の既存送信権限は制御していません。さらに `src/wolfbot/services/discord_service.py:349-372` はメイン text 上の投稿であれば、`author_seat` が `None` でも `LLMAdapter.maybe_react_to_message()` に渡します。`src/wolfbot/services/llm_service.py:432-468` でも非参加者投稿を除外していません。

Fix Direction:
メイン text では「参加中の生存者だけ送信可」を bot 側で強制するべきです。最低でも `@everyone` を read-only にし、参加者だけに `send_messages=True` を付けるか、専用 role を作ってその role で送信権限を管理する構成にする必要があります。加えて `WolfCog.on_message()` 側でも、`author_seat is None` の投稿は LLM 反応入力から除外した方が安全です。

Test Gap:
`tests/test_permission_manager.py` は着席者に対する個別 overwrite の確認しかしておらず、非参加者や `@everyone` の送信権限を検証していません。`tests/test_llm_trigger.py` にも `author_seat=None` の投稿を弾くケースがありません。

### Medium: 起動時 recovery を完了前に one-shot 扱いしており、初回失敗時に同一プロセス内で再試行できない

Impact:
初回 `on_ready` で DB 一時障害や `recover_all()` の想定外例外が起きた場合、その後の再接続や `on_ready` 再発火では recovery が再実行されません。結果として、active game が残っていても engine 未接続のまま放置される可能性があります。

Evidence:
`src/wolfbot/main.py:74-85` では `recovery_done.set()` を `await recovery.recover_all()` より先に実行しています。`RecoveryService.recover_all()` はゲーム単位の失敗は握りつぶしますが、`load_active_games()` 失敗のような全体例外までは吸収していません。したがって recovery 本体が途中で例外終了すると、成功していないのに `recovery_done` だけが立った状態になります。

Fix Direction:
`recovery_done.set()` は `recover_all()` 成功後に移すべきです。再入防止と失敗時再試行を両立したいなら、`asyncio.Lock` か単発タスク参照で多重実行だけ防ぎ、例外時はフラグを残さない構造にした方が安全です。

Test Gap:
`tests/test_recovery.py:236-257` は `RecoveryService.recover_all()` を直接 2 回呼ぶ idempotency だけを見ており、`main.py` の `on_ready` ゲート順序は通っていません。

### Medium: `/wolf start` の DM 事前確認が `create_dm()` だけで、実送信不能を開始前に検知できていない

Impact:
`/wolf start` は「DM を開放しているか」を事前確認しているつもりでも、実際には DM チャンネル作成成功しか見ていません。開始後の役職通知や投票 DM が `discord.Forbidden` で落ちてもゲーム自体は進んでしまうため、特定プレイヤーだけ秘密情報や入力 UI を受け取れない壊れた村が成立します。

Evidence:
`src/wolfbot/services/discord_service.py:509-517` は開始前チェックとして `_preflight_dms()` を使いますが、その実装は `src/wolfbot/services/discord_service.py:667-684` の通り `user.create_dm()` 成功までしか確認していません。一方、実際の DM 送信は `src/wolfbot/services/discord_service.py:136-153`、`src/wolfbot/services/discord_service.py:155-184`、`src/wolfbot/services/discord_service.py:186-240` で行っており、ここでは `discord.Forbidden` を受けてログだけ残して継続します。仕様側の `prompts/IMPLEMENTATION_PROMPT.md:128` は「DM が使えないユーザーにはエラーを返し、ゲーム開始前に検知できるようにする」としており、現状の事前確認では足りません。

Fix Direction:
開始前にテスト用 DM を実送信して削除するか、少なくとも送信 API と同等の経路で `Forbidden` を確認する必要があります。設計上それが難しいなら、開始時点で全参加者に「役職通知送信成功」を強制し、1 人でも失敗したらゲーム開始をロールバックする方が仕様に近いです。

Test Gap:
`_preflight_dms()` を直接検証するテストがありません。`create_dm()` は成功するが `user.send()` は `Forbidden` になるケースを追加しないと、現在の抜けは再発します。

## 実施した確認

- `uv run pytest tests -q` -> `208 passed, 1 warning in 1.17s`
- `uv run mypy` -> `Success: no issues found in 26 source files`
- `uv run ruff check src tests` -> `All checks passed!`
- 重点確認ファイル:
  - `src/wolfbot/services/discord_service.py`
  - `src/wolfbot/services/permission_manager.py`
  - `src/wolfbot/services/llm_service.py`
  - `src/wolfbot/services/recovery_service.py`
  - `src/wolfbot/main.py`
  - `tests/test_permission_manager.py`
  - `tests/test_recovery.py`
  - `tests/test_llm_trigger.py`

## 残留リスク

- `PermissionManager` は着席者に対する individual overwrite を丁寧に管理している反面、既存チャンネルの基底権限に強く依存します。運用環境ごとの差分が大きいので、チャンネル前提を README 系ドキュメントにも明文化した方が安全です。
- Recovery と Discord 実送信の両方で「ログだけ出して継続する」箇所が多いため、運用時は監視なしだと静かに壊れやすいです。今回の 3 件を直しても、ログ監視がない限り発見は遅れます。
