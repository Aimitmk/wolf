# 人狼bot コードレビュー結果

## 概要

- 対象: `src/wolfbot`, `tests`, `CLAUDE.md`, `prompts/IMPLEMENTATION_PROMPT.md`
- 観点: 進行制御、復旧、ギルド単位の整合性、Discord 境界での運用安全性
- 結論: テスト・静的解析は通過している一方で、実運用で破綻しうる高優先度の問題が 2 件、運用時の誤誘導または過剰 API 呼び出しにつながる中優先度の問題が 2 件あります

## 実施した確認

- `uv run pytest tests` → `138 passed in 0.62s`
- `uv run ruff check src tests` → `All checks passed!`
- `uv run mypy` → `Success: no issues found in 24 source files`
- 追加で一時 SQLite DB を使った非破壊の確認を行い、同一 guild の active game 多重化、recovery 再実行時の engine 多重起動、recovery 後の pending 情報の誤りを再現

## 指摘事項

### High: 同一 guild で複数の active game を作れてしまう

- 影響: `/wolf create` が同時実行されると同一 guild に複数の未終了ゲームが残りえます。その後の `/wolf join` `/wolf start` `/wolf status` `on_message` は `load_active_game_for_guild()` が返した 1 件にだけ作用するため、もう片方のゲームは実質的に孤立し、チャンネルや DB 状態が食い違います。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:327`
  - `src/wolfbot/services/discord_service.py:360`
  - `src/wolfbot/persistence/sqlite_repo.py:194`
  - `src/wolfbot/persistence/schema.py:14`
- 詳細: `/wolf create` は active game の存在確認と `create_game()` を分離して実行していますが、`games` テーブルには `guild_id` と `ended_at IS NULL` を束ねる一意制約がありません。`load_active_game_for_guild()` も `LIMIT 1` のみで順序保証がなく、重複発生後の参照先は不定です。
- 再現:
  - 一時 DB で同一 `guild_id` の active game を 2 件作成できました。
  - `load_active_game_for_guild("guild")` はそのうち 1 件だけを返し、もう 1 件は active のまま残りました。
- 改善案:
  - `games` に「guild ごとに active game は 1 件まで」を強制する DB 制約を入れるべきです。
  - `/wolf create` 側の read-then-insert 前提ではなく、書き込み時に競合を確実に弾く設計に寄せるべきです。

### High: reconnect / `on_ready` 再発火で `GameEngine` が多重起動し、古い task が孤児化する

- 影響: Discord 再接続などで `on_ready` が複数回発火すると recovery が再実行され、同じゲームに対して複数の engine task が生き続けます。以後は重複した deadline 監視と `advance()` 呼び出しが走り、shutdown 時も registry に残っていない古い engine は停止対象から漏れます。
- 根拠コード:
  - `src/wolfbot/main.py:76`
  - `src/wolfbot/services/recovery_service.py:52`
  - `src/wolfbot/services/recovery_service.py:103`
  - `src/wolfbot/services/timer_service.py:117`
- 詳細: `recover_all()` は毎回新しい `GameEngine` を生成して `EngineRegistry.attach()` に渡しますが、`attach()` は既存 engine を停止せず dict を上書きするだけです。`on_ready` 側にも recovery を一度だけに抑えるガードがありません。
- 再現:
  - 同じ active game に対して `recover_all()` を 2 回呼ぶと、registry 上の engine オブジェクトは置き換わる一方、最初の engine task も完了せず生存したままでした。
- 改善案:
  - `on_ready` で recovery を一度だけに制限するか、再実行前に既存 engine を停止・再利用するべきです。
  - `EngineRegistry.attach()` で同一 `game_id` の既存 engine を明示的に停止する設計にするのが安全です。

### Medium: restart 後の `WAITING_HOST_DECISION` に保存される pending 情報が実際の未提出者を表していない

- 影響: 復旧後のホスト向け案内、`/wolf status`、`announce_recovery()` の内容が誤ります。すでに投票済みの参加者まで「未提出」と表示されたり、夜フェイズでは実際に足りない役職に関係なく `WOLF_ATTACK` 固定で案内されたりするため、ホスト判断を誤誘導します。
- 根拠コード:
  - `src/wolfbot/services/recovery_service.py:67`
  - `src/wolfbot/services/recovery_service.py:76`
  - `src/wolfbot/services/recovery_service.py:112`
  - `src/wolfbot/services/recovery_service.py:140`
- 詳細: `_derive_pending()` は既存の `votes` / `night_actions` を一切参照せず、`DAY_VOTE` / `DAY_RUNOFF` では「生存者全員」を pending とし、`NIGHT` では「生存する狼・占い・騎士全員」を `WOLF_ATTACK` 扱いで pending にします。コメントにも `caller can refine` とありますが、その refine は実装されていません。
- 再現:
  - 3 人がすでに投票済みの `DAY_VOTE` を deadline 超過で recovery すると、保存された `missing_seats` は `1..9` になりました。
- 改善案:
  - recovery 時は現在の phase/day に対応する保存済み提出を読み、未提出者だけを `PendingDecision` に反映するべきです。
  - `required_submission` も「いま未確定の行動種別」を実データから導出するべきです。

### Medium: `PermissionManager` は idempotent と説明されているが、実装は毎回全 overwrite を再送している

- 影響: recovery や phase 遷移のたびに、差分がなくても全メンバー分の `set_permissions()` が再送されます。9 人村でも main/heaven/wolves の各チャンネルに対して繰り返し API を叩くため、接続不安定時や複数ゲーム運用時には不要な rate limit 圧力になります。
- 根拠コード:
  - `src/wolfbot/services/permission_manager.py:1`
  - `src/wolfbot/services/permission_manager.py:41`
  - `src/wolfbot/services/permission_manager.py:212`
- 詳細: ファイル冒頭コメントは「actual diffs のときだけ API call する」と説明していますが、実装は現在の overwrite 状態を読まずに `apply()` のたび全員へ `channel.set_permissions(...)` を送っています。
- 改善案:
  - コメントどおり idempotent にしたいなら、現在の overwrite と期待値を比較して差分だけ送るべきです。
  - そこまで実装しないなら、少なくとも「常に再送する」ことが分かるようにコメントを修正した方が安全です。

## 補足

- 現状のテストは通常フローと主要な状態遷移をかなり厚くカバーしています。
- ただし今回の指摘は、いずれも「同時実行」「再接続後の復旧」「ホスト運用時の整合性」「外部 API 呼び出し量」といった異常系または運用系の論点で、既存テストでは拾われていません。
- 優先順位は、まず active game の一意性確保、次に recovery / engine の重複起動防止、その後に pending 復元精度と permission diff 化の順が妥当です。
