# 人狼bot コードレビュー結果

## 概要

- 対象コミット: `9fd2be0`
- 対象状態: 上記 `HEAD` に未コミット変更を含む現在のワークツリー
- 対象: `src/wolfbot`, `tests`, `README.md`, `CLAUDE.md`
- 観点: 進行制御、復旧、永続化、Discord/LLM 境界、運用安全性
- 結論: 自動チェックはすべて通過していますが、既存 SQLite DB の起動互換性を壊す高優先度の問題が 1 件、夜行動の進行タイミングと提出 validation に関する中優先度の問題が 2 件あります。

## 実施した確認

- `uv run pytest tests -q` -> `337 passed, 1 warning in 2.10s`
- `uv run ruff check src tests` -> `All checks passed!`
- `uv run mypy` -> `Success: no issues found in 26 source files`
- 追加で、一時 SQLite DB に旧スキーマ相当の `games` / `seats` / `pending_decisions` を作成し、`migrate()` 後の列追加状況と `SqliteRepo.create_game()` の挙動を確認しました。

## 指摘事項

### High: 旧 SQLite スキーマに対する additive migration が不足しており、既存 DB で起動後の通常書き込みが失敗する

- 影響:
  既存の `wolfbot.db` が `force_skip_pending` 追加前の `games` テーブルを持っている場合、現在の `migrate()` は `pending_decisions.submissions_json` しか `ALTER TABLE` しません。そのため、起動時 migration は成功しても、その後の `/wolf create` 相当の `create_game()` が `OperationalError: table games has no column named force_skip_pending` で失敗します。`seats.dm_channel_id` も同じく旧 DB には追加されないため、既存ゲームを読み込む経路で壊れる可能性があります。
- 根拠コード:
  - `src/wolfbot/persistence/schema.py:14-28`
  - `src/wolfbot/persistence/schema.py:154-169`
  - `src/wolfbot/persistence/sqlite_repo.py:55-70`
  - `src/wolfbot/persistence/sqlite_repo.py:199-224`
- 再現:
  旧 `games` テーブルを `force_skip_pending` なしで作成し、`migrate()` を実行してから `SqliteRepo.create_game()` を呼ぶと、次のエラーを確認しました。

  ```text
  OperationalError table games has no column named force_skip_pending
  ```

  また、旧 `games` / `seats` / `pending_decisions` を作って `migrate()` 後の列一覧を確認すると、`pending_decisions.submissions_json` だけが追加され、`games.force_skip_pending` と `seats.dm_channel_id` は追加されていませんでした。
- 詳細:
  `CREATE TABLE IF NOT EXISTS` は既存テーブルの列定義を更新しません。現行コードは DDL に新列を持っていますが、旧 DB に対しては `PRAGMA table_info(pending_decisions)` のみを見て `submissions_json` だけを補完しています。`CLAUDE.md` でも列追加は nullable/defaulted column として既存 DB が upgrade できる形にする方針になっているため、この migration 漏れは運用上の互換性バグです。
- 推奨修正:
  `migrate()` で `games` と `seats` も `PRAGMA table_info` を確認し、存在しない場合は次を追加してください。

  ```sql
  ALTER TABLE games ADD COLUMN force_skip_pending INTEGER NOT NULL DEFAULT 0;
  ALTER TABLE seats ADD COLUMN dm_channel_id TEXT;
  ```

  既存行の読み込みと新規作成の両方を確認する migration regression test も追加するべきです。

### Medium: 狼襲撃の split が「締切時点」ではなく提出完了直後に `WAITING_HOST_DECISION` へ入る

- 影響:
  生存人狼 2 名が別々の襲撃先を提出すると、全夜行動が揃った時点で `_all_night_actions_in()` が `True` を返して engine を wake します。その直後の `advance()` が split を検出し、夜の締切を待たずに `WAITING_HOST_DECISION` へ入ります。仕様コメント上は「締切時点では未確定」とされているため、締切前に狼が自発的に変更して揃える余地が失われます。
- 根拠コード:
  - `src/wolfbot/services/game_service.py:510-521`
  - `src/wolfbot/services/game_service.py:640-656`
  - `src/wolfbot/domain/state_machine.py:566-624`
- 詳細:
  `_all_night_actions_in()` は「必要な actor/kind の行が存在するか」だけを見ており、狼の攻撃対象が一致しているかを見ません。一方、`plan_night_resolve()` は split を `WAITING_HOST_DECISION` の条件にします。結果として、split は「未提出」ではないため早期 wake され、早期 wake 後に「未確定」として停止します。
- 推奨修正:
  早期 wake 判定では、狼襲撃が split の場合は `False` を返し、締切または明示的な host 操作まで待つべきです。具体的には `_all_night_actions_in()` で現在の `NightAction` と生存狼を使って `resolve_wolf_attack(..., force_skip=False)` を呼び、`attack.split` の場合は wake しないようにします。
- テストギャップ:
  2 狼が別ターゲットを提出した直後に `wake()` されないこと、締切到達後に `WAITING_HOST_DECISION` へ入ることを確認する service-level test がありません。

### Medium: `submit_night_action()` が `target_seat=None` を能力提出として受け付け、占い・護衛などを未提出扱いにしなくなる

- 影響:
  `submit_night_action()` は `target_seat is not None` の場合だけ合法対象 validation を行い、`None` はそのまま保存します。そのため、占い師や騎士の action が `target_seat=None` で入ると、`plan_night_resolve()` 側では「提出済み」とみなされ、待機にもならず、占い結果や護衛効果だけが発生しない状態になります。LLM の fallback や現在の UI は通常 `None` を出しにくい構造ですが、service 境界としては不正提出を受け入れています。
- 根拠コード:
  - `src/wolfbot/services/game_service.py:491-510`
  - `src/wolfbot/services/game_service.py:510-521`
  - `src/wolfbot/domain/state_machine.py:552-563`
  - `src/wolfbot/domain/state_machine.py:679-683`
- 詳細:
  vote は棄権として `target_seat=None` を意味づけていますが、夜行動には同等の skip UI がありません。狼の force-skip 時の「行動なし」と、通常提出時の `None` を同じ保存形式で許すと、通常夜の能力欠落が検出されません。
- 推奨修正:
  通常の `submit_night_action()` では `target_seat=None` を `SubmitResult.ILLEGAL_TARGET` などで拒否するべきです。未提出を強制的に行動なし扱いにする処理は、`force_skip_pending` を見た解決ロジック内に閉じ込め、通常提出 API からは保存しない設計に寄せるのが安全です。
- テストギャップ:
  `SEER_DIVINE` / `KNIGHT_GUARD` / `WOLF_ATTACK` の通常提出で `target_seat=None` が拒否されること、拒否時に `night_actions` 行が増えないことを確認するテストがありません。

## 補足

- 直近レビューで指摘されていた `/wolf start` と `/wolf leave` の競合による 8 席開始、非参加者メインチャンネル投稿による LLM 反応、手動作成の同名 `wolf-heaven` / `wolf-wolves` 削除、LLM cooldown の投稿時刻ずれは、現行コードでは対策が入っていることを確認しました。
- テスト数は増えており、通常の状態遷移、復旧、LLM prompt、Discord message filter はかなり厚くなっています。今回の残りは主に「旧 DB 互換」「早期 wake と仕様文のずれ」「service 境界の防御」の問題です。

## 追加推奨テスト

- 旧 `games` / `seats` / `pending_decisions` テーブルを作成してから `migrate()` し、`games.force_skip_pending`、`seats.dm_channel_id`、`pending_decisions.submissions_json` が追加されること。
- 旧 DB から migration 後、`SqliteRepo.create_game()` と `load_game()` / `load_players()` が例外なく動くこと。
- 2 狼が別ターゲットを提出しても締切前には早期 wake せず、締切後に `WAITING_HOST_DECISION` へ入ること。
- `submit_night_action(..., target_seat=None)` が通常夜行動として保存されないこと。
