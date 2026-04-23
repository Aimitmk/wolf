# 人狼bot コードレビュー結果

## 概要

- 対象: `src/wolfbot`, `tests`, `CLAUDE.md`, `prompts/IMPLEMENTATION_PROMPT.md`
- 観点: ゲーム進行、DM UI の有効期限、再起動復旧、LLM 補完と発言制御の運用安全性
- 結論: テスト・静的解析は通過している一方で、実運用で破綻しうる高優先度の問題が 3 件、中優先度の問題が 1 件あります

## 実施した確認

- `uv run pytest tests` → `163 passed, 1 warning in 1.75s`
- `uv run ruff check src tests` → `All checks passed!`
- `uv run mypy` → `Success: no issues found in 26 source files`
- 追加で一時 SQLite DB を使った非破壊の確認を行い、以下を再現しました
  - 人間 7 人以下での `/wolf start` による LLM 補完失敗
  - 古い DM を翌日の同一フェイズで押したときの stale 提出受理
  - 人狼 2 人の襲撃先 split 状態での recovery / `/wolf extend` 後の再提出不能
  - 同一 LLM への同時トリガによる発言 cap / cooldown の破り

## Findings

### High: `/wolf start` の LLM 補完が 2 人以上だと座席番号計算に失敗して開始できない

- 影響:
  - 人間が 7 人以下の状態で開始すると、必要な LLM が 2 人以上になるケースでゲーム開始処理が落ちます。
  - 仕様上は「不足人数分を LLM で補完」すべきですが、実際には一部の人数帯で開始不能です。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:463`
  - `src/wolfbot/services/discord_service.py:467`
- 詳細:
  - LLM 補完ループ内で `seat_no = _next_seat_no(seats) + i` を使っています。
  - 各周回で `seats` を再読込しているため、`_next_seat_no(seats)` 自体がすでに次の空き座席を返します。そこにさらに `+ i` を足すと座席番号を飛ばします。
  - 7 人開始のケースでは 1 人目が `8`、2 人目が `10` になり、`Seat(seat_no=10)` の Pydantic validation error で処理が落ちます。
- 再現:
  - 補完ロジックを同じ計算式で最小再現すると `computed 0 8`, `computed 1 10` となり、2 回目で `seat_no <= 9` 制約違反になりました。
- 改善案:
  - ループごとに `_next_seat_no(seats)` をそのまま採用し、`+ i` をやめるべきです。
  - 7 人、6 人など「2 人以上補完」の開始ケースを回帰テストに追加した方が安全です。

### High: 古い DM を翌日の同一フェイズで押すと stale ではなく現在日の提出として受理される

- 影響:
  - たとえば day 1 の投票 DM を保持したまま day 2 の `DAY_VOTE` で押すと、その操作が day 2 の有効な投票として保存されます。
  - 夜行動でも同じで、前夜の DM を翌夜の `NIGHT` で使えてしまいます。古い UI が誤って現在日の行動を上書きするため、提出経路の整合性が壊れます。
- 根拠コード:
  - `src/wolfbot/ui/views.py:53`
  - `src/wolfbot/ui/views.py:102`
  - `src/wolfbot/services/game_service.py:357`
  - `src/wolfbot/services/game_service.py:418`
- 詳細:
  - `VoteView` / `NightActionView` は `game_id`, `seat`, `round_` / `kind` は保持していますが、`day` を持っていません。
  - `submit_vote()` / `submit_night_action()` は current phase だけを見て stale 判定しており、「その DM がどの日付の UI か」は検証していません。
  - そのため、同じ phase に戻ってきた別日では古い DM がそのまま current `game.day_number` で保存されます。
- 再現:
  - `DAY_VOTE day 1` のゲームを DB 上で `DAY_VOTE day 2` に進めた後、`submit_vote(..., round_=0)` を呼ぶと `ACCEPTED` になり、`day=2` の投票として保存されました。
  - `NIGHT day 1` を `NIGHT day 2` に進めた後、`submit_night_action(..., SEER_DIVINE, ...)` を呼ぶと同様に `ACCEPTED` となり、`day=2` の夜行動として保存されました。
- 改善案:
  - DM View に `day` を持たせ、service 側で `game.day_number` と一致しなければ `STALE_PHASE` 相当で reject するべきです。
  - テストは「phase は同じだが day が異なる古い DM」を明示的に追加した方がよいです。

### High: 人狼 2 人の襲撃先 split は recovery / `/wolf extend` 後に再提出できず詰む

- 影響:
  - 仕様どおり「1 対 1 で割れたら `WAITING_HOST_DECISION`」までは入りますが、その後にホストが `/wolf extend` しても狼へ再提出 DM が飛びません。
  - そのため split 状態を人間操作で解消できず、実質的に `/wolf force-skip` しか使えません。
- 根拠コード:
  - `src/wolfbot/domain/state_machine.py:489`
  - `src/wolfbot/services/recovery_service.py:72`
  - `src/wolfbot/services/submission_snapshot.py:48`
  - `src/wolfbot/services/game_service.py:492`
- 詳細:
  - 通常進行では `plan_night_resolve()` が `attack.split` を見て `WAITING_HOST_DECISION` に落とし、`pending` には `WOLF_ATTACK` と狼 2 人を入れます。
  - しかし recovery 時の `derive_pending()` / `missing_submitters()` は「未提出 seat」しか見ておらず、split のような「全員提出済みだが未確定」の状態を表現できません。
  - その結果、deadline 超過後の recovery では `pending.required_submission=WOLF_ATTACK` なのに `missing_seats=()` という空の pending になります。
  - さらに `/wolf extend` 後の `resend_pending_dms()` も `missing_submitters()` に依存しているため、狼への再提出 DM が送られません。
- 再現:
  - 夜に狼 1 が seat 7、狼 2 が seat 8 を襲撃し、占い師と騎士は提出済みの状態で recovery をかけると、保存された pending は `WOLF_ATTACK` かつ `missing_seats=()` でした。
  - その WAITING 状態に対して `host_extend()` を実行しても `send_night_action_dms` は 0 回でした。
- 改善案:
  - `PendingDecision` と `submission_snapshot` に「未提出」だけでなく「提出済みだが未確定」の split 状態を持たせる必要があります。
  - `/wolf extend` 後は、split 中の狼に対して再提出用 DM を明示的に再送するべきです。
  - recovery 経由と通常 `advance()` 経由の両方で同じ pending 表現になるよう統一した方が安全です。

### Medium: LLM の発言 cap / cooldown が並行トリガで破られ、同一 LLM が二重発言できる

- 影響:
  - 人間メッセージが短時間に複数来たとき、同じ LLM が cooldown 中にもかかわらず複数回発言できます。
  - 昼の発言上限 `NORMAL_SPEECH_CAP=3` も seat 単位で厳密には守られず、ノイズ投稿が増えます。
- 根拠コード:
  - `src/wolfbot/services/llm_service.py:327`
  - `src/wolfbot/services/llm_service.py:350`
  - `src/wolfbot/services/llm_service.py:365`
  - `src/wolfbot/services/llm_service.py:410`
- 詳細:
  - `maybe_react_to_message()` は `load_llm_speech()` で count / cooldown を読んだあと、その seat に対する排他なしで `_maybe_speak()` を呼びます。
  - `_maybe_speak()` 側でも再確認はしますが、ここでも read-check-write が非原子的です。
  - 2 本のトリガが同時に走ると、両方が「まだ話していない」と判断したまま LLM 呼び出しと投稿を完了できます。
- 再現:
  - 同一 LLM に対して `maybe_react_to_message()` を `asyncio.gather()` で 2 本同時に流すと、同じ内容が 2 投稿され、speech count は `(2, False, 1000)` になりました。
- 改善案:
  - LLM seat ごとに `asyncio.Lock` を持ち、reactive speech は seat 単位で直列化するべきです。
  - あるいは DB 側で compare-and-set 的に speech count を更新し、投稿前に予約を取る方式にする必要があります。

## 補足

- 既存テストは通常フロー、復旧、permission、stale phase のような主要パスをかなり厚く押さえています。
- 一方で今回の 4 件は、いずれも「同じ phase の別日」「split だが未提出ではない」「並行トリガ」「複数 LLM 補完」といった運用境界の欠陥で、既存テストでは拾われていません。
- 優先順位は、まず LLM 補完の座席計算と stale DM の day 検証、その後に wolf split の再提出経路、最後に LLM 発言の排他制御が妥当です。
