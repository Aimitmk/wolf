# 人狼bot コードレビュー結果

## 概要

- 対象: `src/wolfbot`, `tests`, `prompts/IMPLEMENTATION_PROMPT.md`
- 観点: フェイズ進行、復旧、DM 再送、LLM 呼び出し、Discord コマンドの競合耐性
- 結論: 静的解析とテストは通過していますが、現行コードでも高優先度 2 件、中優先度 1 件の問題が残っています

## 実施した確認

- `uv run pytest tests -q` → `203 passed, 1 warning in 1.33s`
- `uv run ruff check src tests` → `All checks passed!`
- `uv run mypy` → `Success: no issues found in 26 source files`
- 重点確認:
  - `src/wolfbot/services/game_service.py`
  - `src/wolfbot/services/discord_service.py`
  - `src/wolfbot/services/llm_service.py`
  - `src/wolfbot/services/recovery_service.py`
  - `src/wolfbot/services/timer_service.py`
  - `src/wolfbot/persistence/sqlite_repo.py`
  - `tests/test_recovery.py`, `tests/test_game_service_advance.py`, `tests/test_views.py`

## Findings

### High: 夜行動 DM の再送が壊れており、再起動復旧や `/wolf extend` 後に正しい候補を出せない

- 影響:
  - `NIGHT` 中に bot が再起動した場合、未提出の占い師や騎士に再送される DM の候補が空、または不正に絞られます。
  - 人狼の襲撃先が割れて `WAITING_HOST_DECISION` に入ったあと `/wolf extend` しても、再送 DM で再提出できず、ホストは実質 `/wolf force-skip` に追い込まれます。
- 根拠コード:
  - `src/wolfbot/services/game_service.py:566-579`
  - `src/wolfbot/services/discord_service.py:186-224`
  - `src/wolfbot/domain/rules.py:48-69`
- 詳細:
  - `GameService.resend_pending_dms()` は夜の再送対象として、未提出者や split 狼だけを `actors` に絞って `send_night_action_dms()` に渡しています。
  - しかし `DiscordBotAdapter.send_night_action_dms()` は、その `players` 引数を「DM を送る相手一覧」と「合法ターゲットを計算する母集団」の両方に使っています。
  - そのため、たとえば split 狼の再送では `players == [狼1, 狼2]` になり、`legal_attack_targets(players, p.seat_no)` は非狼の生存者を 1 人も見つけられず、候補が空になります。占い師や騎士の再送でも同じで、未提出者だけを見て合法対象を計算するため候補が欠落します。
- 発生シナリオ:
  - 例 1: 生存狼 2 人が別々の襲撃先を選び、`/wolf extend` で再送 DM を期待する。
  - 例 2: `NIGHT` 中に bot を再起動し、占い師だけ未提出の状態で recovery が `resend_pending_dms()` を呼ぶ。
  - いずれも再送先自体は記録されますが、実際の DM view は空候補または欠落候補になり、再提出経路として成立しません。
- 改善案:
  - `send_night_action_dms()` の API を分けて、「DM を送る actor 一覧」と「合法候補を計算する全生存者一覧」を別引数にするべきです。
  - あわせて、再送テストは受信者の seat 番号だけでなく、Select の候補内容まで検証すべきです。現在の `tests/test_recovery.py:504-575` は送信先しか見ていないため、この不具合を捕まえられていません。

### High: 投票/夜行動フェイズ開始時に LLM API を同期で待つため、xAI の遅延だけで締切監視が止まる

- 影響:
  - `DAY_VOTE` / `DAY_RUNOFF` / `NIGHT` へ遷移した直後、xAI 側の応答が遅いだけで `GameService.advance()` が長時間戻らず、`GameEngine` が次の締切監視に戻れません。
  - 結果として、Discord 側では投票 DM が送られていても、実際の締切処理は LLM API 待ちに引きずられて数十秒から数分単位で遅延します。
- 根拠コード:
  - `src/wolfbot/services/game_service.py:215-227`
  - `src/wolfbot/services/game_service.py:286-330`
  - `src/wolfbot/services/llm_service.py:124-146`
  - `src/wolfbot/services/llm_service.py:216-305`
  - `src/wolfbot/services/timer_service.py:76-89`
- 詳細:
  - `GameService._advance_once()` は phase commit 後に `_dispatch_submissions()` を `await` しています。
  - `_dispatch_submissions()` は `DAY_VOTE` / `DAY_RUNOFF` / `NIGHT` で `self.llm.submit_llm_*()` を同期で待ち、その内部では各 LLM を逐次処理しています。
  - `XAILLMActionDecider.decide()` は 30 秒 timeout + 最大 4 回 retry なので、1 体の LLM でも長時間ブロックし得ます。複数 LLM がいる村ではその待ち時間が直列に積み上がります。
  - `GameEngine` は `advance()` が返るまで次の deadline 監視ループに戻れないため、phase entry 中の LLM API 待ちが、そのままゲーム全体の進行停止になります。
- 発生シナリオ:
  - 人間 5 人 + LLM 4 人の村で `DAY_VOTE` に入る。
  - xAI が一時的に重く、1 体ごとの structured output が timeout/retry を繰り返す。
  - 人間側は既に DM を受け取っていても、engine は phase entry の `advance()` から戻れず、deadline 到達時刻を過ぎても解決処理が走らない。
- 改善案:
  - LLM 投票/夜行動は phase entry のクリティカルパスから外し、バックグラウンド task 化するべきです。
  - 少なくとも「人間 DM 配布」と「engine の再スケジュール」を先に終わらせたうえで、LLM は別 task で submit する構造にしないと、外部 API 遅延がそのままゲーム停止要因になります。
  - 追加テストとして、遅延する fake decider を使い「人間 DM は即時に送られ、engine は締切監視を継続できる」ことを検証すべきです。現状のテストにはこの系統のケースがありません。

### Medium: `/wolf abort` が `host_abort()` の結果を無視して成功扱いし、競合時に registry を誤って切り離す

- 影響:
  - `/wolf abort` がレースに負けて実際には abort できなかった場合でも、ユーザーには必ず成功メッセージが返ります。
  - そのうえ `registry.detach(game.id)` が無条件に走るため、まだ進行中だったゲームの engine 参照を registry から外し、以後の `wake()` ルーティングを壊す可能性があります。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:607-629`
  - `src/wolfbot/services/game_service.py:656-667`
  - `src/wolfbot/services/timer_service.py:132-138`
- 詳細:
  - `WolfCog.abort()` は `await self.gs.host_abort(game.id)` の戻り値を見ず、常に `registry.detach(game.id)` と成功メッセージを実行しています。
  - `host_abort()` 自体は `game is None` または `ended_at is not None` なら `False` を返す設計です。
  - つまり、別経路で直前にゲームが終了した競合ケースでは、abort 失敗にもかかわらず「成功」と表示し、registry だけ切り離す不整合が起こり得ます。
- 発生シナリオ:
  - 管理者の `/wolf abort` と、別タスクの victory/end 処理がほぼ同時に走る。
  - `host_abort()` は `False` を返すが、slash command 側は無条件で成功応答し、registry から engine を外す。
- 改善案:
  - `host_abort()` の戻り値を評価し、失敗時は他コマンドと同様にエラーメッセージを返すべきです。
  - 成功時にのみ `detach()` し、必要なら `detach()` 後に返ってきた engine を `stop()` まで行う方が安全です。

## Open Questions / Residual Risks

- `EngineRegistry` と `GameService._advance_locks` は、終了済みゲームの ID を明示的に掃除していません。現状は即時の機能不全には直結しませんが、長期稼働での線形成長は残っています。
- 既存の `reviews/` 配下の一部文書は現行コードと食い違います。今回のレビュー結果を正とし、過去文書の指摘はそのまま再利用しない方が安全です。

## 短い総括

通常フローの状態遷移、秘密チャンネルの後始末、stale DM への応答改善など、以前の主要問題はかなり潰れています。現時点で優先度が高いのは、夜行動 DM 再送の壊れを直して recovery/extend を実運用で成立させることと、LLM API 遅延を phase 進行のクリティカルパスから外すことです。
