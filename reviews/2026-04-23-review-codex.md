# 人狼bot コードレビュー結果 (Codex)

## Overview

- 対象: `src/wolfbot`, `tests`, `CLAUDE.md`
- 観点: 進行制御の競合安全性、ホスト向け運用可視性、現行テストと実装の整合
- 結論: レイヤ分離とテスト整備は良好ですが、現状でも運用上無視しにくい問題が 2 件あります。特に `/wolf start` の開始処理は、競合時に例外で落ちうえ、勝者が確定する前にロビー状態を書き換えます。

## 実施した確認

- `uv run pytest tests` → `176 passed, 1 warning in 2.06s`
- `uv run ruff check src tests` → `All checks passed!`
- `uv run mypy` → `Success: no issues found in 26 source files`
- 追加で一時 SQLite DB を使った非破壊の再現確認を実施
  - `_backfill_llm_seats()` を 9 人未満のロビーに対して 2 回走らせると `IntegrityError UNIQUE constraint failed: seats.game_id, seats.seat_no` が発生
  - その時点で座席追加は個別 commit 済みのため、開始競合の敗者側でも途中までロビー状態を変更しうることを確認

## Findings

### High: `/wolf start` が LLM 補完を楽観ロック前に実行しており、競合時に生の例外と非原子的なロビー更新を起こす

- 症状: `/wolf start` は `LOBBY -> SETUP` の `apply_transition(... expected_phase=Phase.LOBBY)` より先に `_backfill_llm_seats()` を呼びます。`_backfill_llm_seats()` は 1 座席ずつ `insert_seat()` / `insert_persona_assignment()` を commit するため、開始権を取れていない呼び出しでもロビーを書き換えられます。
- 影響: ホストの二重実行や再送で開始処理が競合すると、敗者側は `IntegrityError` で slash command ごと失敗しえます。さらに、どちらの呼び出しが入れた LLM 席かが混ざるため、開始前の状態遷移と座席補完が一体として扱われていません。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:466-487`
  - `src/wolfbot/services/discord_service.py:683-710`
  - `src/wolfbot/persistence/sqlite_repo.py:262-276`
- 再現メモ:
  - 人間 5 席のロビーを用意し、`_backfill_llm_seats(repo, game_id, 4, rng)` を 2 回呼ぶと 2 回目で `UNIQUE constraint failed: seats.game_id, seats.seat_no` になりました。
  - これは `/wolf start` 本体でも同じ順序で呼ばれているため、開始競合時に同種の失敗が起きます。
- 修正方針:
  - `LOBBY -> SETUP` の勝者を先に確定してから LLM 補完を行うべきです。
  - あるいは「開始権取得 + LLM 席補完」を 1 つの service/repo 操作として直列化し、少なくとも敗者側がロビーを変更できない形にするべきです。

### Medium: `/wolf status` が `unresolved_seats` を表示しないため、狼襲撃の割れによる再提出待ちをホストが把握しにくい

- 症状: `WAITING_HOST_DECISION` 中の `PendingDecision` は `missing_seats` と `unresolved_seats` を保持していますが、`/wolf status` は `missing_seats` しか表示しません。一方で `announce_waiting()` は両方を表示しています。
- 影響: 2 狼の襲撃先が割れて `missing_seats=()` / `unresolved_seats=(1, 2)` になったケースでは、`/wolf status` だけ見ると未提出がないように見えます。ホストが `/wolf extend` すべき状況を読み違えやすいです。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:211-237`
  - `src/wolfbot/services/discord_service.py:536-545`
  - `src/wolfbot/domain/models.py:92-138`
  - `src/wolfbot/domain/state_machine.py:493-547`
- 修正方針:
  - `/wolf status` でも `announce_waiting()` と同じ粒度で `missing_seats` と `unresolved_seats` の両方を表示するべきです。
  - 少なくとも `未提出` という見出しだけではなく、`未提出` / `再提出待ち` を分けるべきです。

## Residual Notes

- `domain` と `services` の分離、`apply_transition(... expected_phase=...)` による楽観ロック、`RecoveryService` と `EngineRegistry` の責務分離は全体として整理されています。
- `pytest` / `ruff` / `mypy` が現状すべて通っており、通常フローの回帰耐性は高いです。
- 今回の指摘はどちらも「異常系の並行実行」または「ホスト運用時の可視性」に寄っており、既存テストが薄い境界に集中しています。
