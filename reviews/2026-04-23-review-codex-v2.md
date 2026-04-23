# 人狼bot コードレビュー結果 (Codex v2)

## Overview

- 対象: `src/wolfbot`, `tests`, `CLAUDE.md`
- 観点: 競合安全性、ホスト操作整合性、対象識別子の設計
- 結論: テストと静的解析はすべて通過していますが、実運用では無視しにくい問題が 3 件残っています。最優先は、`/wolf start` とロビー参加・退出操作の競合でゲーム状態を壊せる点です。

## 実施した確認

- `uv run pytest tests -q` → `184 passed, 1 warning in 2.01s`
- `uv run ruff check src tests` → `All checks passed!`
- `uv run mypy` → `Success: no issues found in 26 source files`
- 追加で一時 SQLite DB を使った非破壊確認を実施
- `claim_start_and_backfill()` 成功後に stale な `delete_seat()` を当てると、開始済みゲームの座席数が 8 に落ちることを確認
- stale な `insert_seat()` は `UNIQUE constraint failed: seats.game_id, seats.seat_no` を返すことを確認
- `force_skip_pending=True` を立てた直後に `/wolf extend` 相当の phase 復帰を先に成功させると、`force_skip_pending` が残留することを確認
- `_resolve_target("Alice", [...])` は同名候補が 2 人いても先頭の 1 人だけを返すことを確認

## Findings

### High: `/wolf join` / `/wolf leave` が `/wolf start` と原子的でなく、開始直後の村を壊せる

- 影響:
  - `/wolf join` と `/wolf leave` は slash command 側で `phase is LOBBY` を確認してから、phase 条件なしの `insert_seat()` / `delete_seat()` を実行します。
  - 一方 `/wolf start` は `claim_start_and_backfill()` で `LOBBY -> SETUP` と LLM 補完を原子的に確定します。
  - そのため、開始前に通った stale な `/wolf leave` が開始後に走ると、`SETUP` に入ったゲームから座席が 1 人消えます。
  - `plan_setup()` は `assign_roles()` を呼び、9 席ちょうどでないと `ValueError` を投げるため、開始成功メッセージの後にセットアップが失敗しうえ、村が壊れたまま残ります。
  - stale な `/wolf join` も、LLM 補完で埋まった座席番号へ `insert_seat()` を打つため、生の `IntegrityError` で interaction ごと落ちます。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:415-467`
  - `src/wolfbot/services/discord_service.py:474-517`
  - `src/wolfbot/persistence/sqlite_repo.py:262-282`
  - `src/wolfbot/persistence/sqlite_repo.py:677-732`
  - `src/wolfbot/domain/state_machine.py:118-128`
  - `src/wolfbot/domain/rules.py:22-28`
- 再現メモ:
  - 8 人ロビーに対して `claim_start_and_backfill(..., llm_seats=[("LLM9", "setsu")])` を成功させたあと、stale な `delete_seat(game_id, 8)` を実行すると座席が `[1, 2, 3, 4, 5, 6, 7, 9]` になりました。
  - 同条件で stale な `insert_seat(seat_no=9)` を実行すると `IntegrityError UNIQUE constraint failed: seats.game_id, seats.seat_no` になりました。
- 改善方針:
  - ロビーの着席・退出を repo 側の原子的な操作に寄せ、`expected_phase=LOBBY` を同一トランザクションで検証するべきです。
  - 少なくとも `insert_seat()` / `delete_seat()` をそのまま slash command から叩かず、開始競合の敗者側は clean に失敗を返すようにするべきです。

### Medium: `/wolf force-skip` が `/wolf extend` に競り負けても `force_skip_pending` を残留させる

- 影響:
  - `host_force_skip()` は `set_force_skip(True)` と phase 復帰を別々の書き込みで行っています。
  - 途中で `/wolf extend` が先に `WAITING_HOST_DECISION -> 元フェイズ` を成功させると、`host_force_skip()` 自体は `False` を返しますが、`force_skip_pending` はそのまま残ります。
  - 以後の `advance()` は `_plan_next()` から `game.force_skip_pending` をそのまま resolver に渡すため、延長したつもりのフェイズが次の締切で強制確定扱いに化けます。
  - これはホストの最終判断を race に依存させるため、`WAITING_HOST_DECISION` の運用上かなり危険です。
- 根拠コード:
  - `src/wolfbot/services/game_service.py:229-270`
  - `src/wolfbot/services/game_service.py:607-653`
  - `src/wolfbot/persistence/sqlite_repo.py:247-252`
  - `src/wolfbot/persistence/sqlite_repo.py:620-621`
- 再現メモ:
  - 一時 DB で `force_skip_pending=True` を先に立て、その後 `/wolf extend` 相当の `apply_transition(... expected_phase=WAITING_HOST_DECISION)` を成功させ、最後に `/wolf force-skip` 側の phase swap を失敗させると、最終状態は `phase=DAY_VOTE`, `deadline=9999`, `force_skip_pending=True` でした。
- 改善方針:
  - `force-skip` は「フラグを立てる」「phase を戻す」を 1 つの compare-and-swap にまとめるべきです。
  - それが難しければ、phase 復帰に失敗したときは `force_skip_pending` を明示的に戻すべきです。

### Medium: 対象選択を `display_name` に依存しており、同名プレイヤーで投票・夜行動の対象が曖昧になる

- 影響:
  - 人間プレイヤーの `display_name` は `interaction.user.display_name` をそのまま保存しており、一意制約も正規化もありません。
  - DM UI の `SelectOption` ラベルは `display_name` だけで、LLM に渡す候補も `target_name=<display_name>` 前提です。
  - `_resolve_target()` は一致した最初の `display_name` を返すだけなので、同名候補が 2 人いると LLM は誰を選んだのか表現できません。
  - UI 側も重複ラベルをそのまま並べるため、人間プレイヤーから見てもどちらの「Alice」なのか判別できません。
  - LLM persona 名は人間名との衝突を避けていないため、人間が `Setsu` や `Gina` などの表示名を使っている場合も同じ曖昧性が起きます。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:438-447`
  - `src/wolfbot/ui/views.py:69-79`
  - `src/wolfbot/ui/views.py:128-135`
  - `src/wolfbot/llm/prompt_builder.py:110-129`
  - `src/wolfbot/services/llm_service.py:223-231`
  - `src/wolfbot/services/llm_service.py:275-285`
  - `src/wolfbot/services/llm_service.py:488-505`
  - `src/wolfbot/llm/personas.py:137-149`
- 再現メモ:
  - `_resolve_target("Alice", [seat 3 "Alice", seat 7 "Alice"], allow_none=False)` は seat 3 を返しました。
  - `VoteView` / `NightActionView` は同名候補がいても同一ラベルをそのまま 2 つ並べます。
- 改善方針:
  - 人間/LLM の候補識別子は `display_name` ではなく、`座席3 Alice` のような安定した in-game token に寄せるべきです。
  - LLM の `target_name` も席番号込みのトークンに変え、復元側はその token を厳密に解決するべきです。
  - 参加時点で表示名の重複を避ける、または自動的に識別 suffix を付ける方針も検討した方が安全です。

## Residual Notes

- `apply_transition()` は現行コードでは単一トランザクション化されており、この点は過去レビュー時点より明確に改善されています。
- recovery 時の pending 復元と DM 再送、`WAITING_HOST_DECISION` の表示改善、`EngineRegistry.attach()` の既存 engine 停止など、以前の運用系の穴もかなり埋まっています。
- 今回の指摘は主に「競合するホスト/参加者操作」と「名前を識別子として使う設計」に集中しており、通常フローの状態遷移そのものはかなり整理されています。
