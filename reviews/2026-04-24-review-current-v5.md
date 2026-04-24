# 人狼bot コードレビュー結果

## 概要

- 対象コミット: `c02121a`
- 対象状態: 上記 `HEAD` に未コミット変更を含む現在のワークツリー
- 対象: `src/wolfbot`, `tests`, `prompts/IMPLEMENTATION_PROMPT.md`, `CLAUDE.md`
- 観点: 進行制御、夜処理のイベント順、永続化、Discord/LLM 境界、再起動復旧、情報漏洩耐性
- 結論: 自動チェックはすべて通過しています。High 相当の起動不能・DB破損は見つかりませんでしたが、夜明けの Discord 投稿順、騎士の前夜護衛履歴、死者発言ログ、LLM 棄権解釈に中〜低優先度の修正余地があります。

## 実施した確認

- `uv run pytest tests -q` -> `400 passed, 1 warning in 2.07s`
- `uv run ruff check src tests` -> `All checks passed!`
- `uv run mypy` -> `Success: no issues found in 26 source files`
- 追加で、Discord app command group の登録挙動をローカル import レベルで確認しました。
- 旧 DB migration については、`games.force_skip_pending`、`seats.dm_channel_id`、`pending_decisions.submissions_json` の additive migration と回帰テストが入っていることを確認しました。

## 指摘事項

### Medium: 夜明けの Discord 投稿順が仕様上の解決順と逆転する

- 影響:
  通常の夜明けでは、メインチャンネルに「N 日目の議論を開始します」が先に投稿され、その後に役職者への private result、最後に朝アナウンスが投稿されます。夜襲撃で勝利が成立した場合は、`VICTORY` / `ROLE_REVEAL` が朝アナウンスより先に出ます。仕様は「霊媒結果 -> 占い結果 -> 護衛/襲撃解決 -> 朝の公開アナウンス -> 勝利判定/次フェイズ」の順を固定しているため、Discord 上の観測順がゲーム内イベント順とずれます。
- 根拠コード:
  - `src/wolfbot/services/game_service.py:205-218`
  - `src/wolfbot/domain/state_machine.py:725-775`
  - `prompts/IMPLEMENTATION_PROMPT.md:338-349`
- 詳細:
  `plan_night_resolve()` は `public_logs` を `MORNING` -> `PHASE_CHANGE`、または `MORNING` -> `VICTORY` -> `ROLE_REVEAL` の順で返しています。しかし `GameService._advance_once()` は `MORNING` をいったん skip し、残りの public log をすべて投稿してから private log を送り、最後に `post_morning()` します。DB 上の log 順と Discord 表示順も一致しません。
- 推奨修正:
  夜明け遷移では private log を先に送ったうえで、`transition.public_logs` を順番どおりに処理し、`entry.kind == "MORNING"` のときだけ `post_morning()` を呼ぶ形に寄せてください。別枠の `transition.morning_text` 投稿は、二重投稿防止を保ったまま削除または `MORNING` entry のレンダリング情報として扱うのが安全です。
- テストギャップ:
  `FakeDiscordAdapter.calls` で `send_private(MEDIUM_RESULT/SEER_RESULT)`、`post_morning`、`post_public(PHASE_CHANGE/VICTORY/ROLE_REVEAL)` の相対順を確認する service-level test がありません。

### Medium: 騎士が force-skip された夜のあと、2 夜前の護衛先が翌夜も禁止候補として残る

- 影響:
  騎士がある夜に護衛を提出せず、ホストが `/wolf force-skip` で「行動なし」として進めた場合、その夜の護衛先は存在しません。しかし `previous_guard` は更新もクリアもされないため、さらに次の夜も古い `last_guard_seat` が候補から除外されます。結果として、本来は合法な護衛先が UI、LLM、service validation の全経路で拒否されます。
- 根拠コード:
  - `src/wolfbot/domain/state_machine.py:679-723`
  - `src/wolfbot/persistence/sqlite_repo.py:783-795`
  - `src/wolfbot/services/game_service.py:512-514`
  - `src/wolfbot/services/discord_service.py:241-263`
  - `src/wolfbot/services/llm_service.py:263-288`
  - `prompts/IMPLEMENTATION_PROMPT.md:297-305`
  - `prompts/IMPLEMENTATION_PROMPT.md:336`
- 詳細:
  `record_guard` は `guard_target is not None` の場合だけ出力され、`apply_transition()` も `record_guard is not None` の場合だけ `previous_guard` を upsert します。一方、次夜の候補計算は `last_guard_day` を見ずに `prev[1]` だけを使っています。そのため「前夜に護衛した相手」ではなく「最後に護衛した相手」が常に禁止されます。
- 推奨修正:
  `load_previous_guard()` の結果を使う側で `prev[2] == game.day_number` のときだけ `prev[1]` を前夜護衛先として扱うか、force-skip/未提出解決時に `last_guard_seat=NULL, last_guard_day=next_day` を記録できるよう `Transition.record_guard` の表現を拡張してください。UI、LLM、service validation の 3 箇所で同じ helper を使うとずれを防げます。
- テストギャップ:
  夜 1 に護衛 A を記録し、夜 2 は騎士未提出を force-skip、夜 3 に A が再び合法候補へ戻ることを確認する regression test がありません。

### Medium: 死亡プレイヤーのメインチャンネル投稿が public log に残り、後続 LLM の文脈に入る

- 影響:
  `on_message()` は `DAY_DISCUSSION` のメインチャンネル投稿について、`author_seat is not None` なら生死を問わず `PLAYER_SPEECH` として保存します。直後の LLM 反応は `_main_channel_should_llm_react()` で止まりますが、保存済み public log は後続の LLM prompt に含まれるため、権限を迂回できる死者や管理者権限を持つ死者が LLM の判断材料を汚染できます。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:461-489`
  - `src/wolfbot/services/llm_service.py:739-766`
  - `tests/test_discord_on_message_filter.py:1-28`
- 詳細:
  既存テストは「死者が即時 LLM 反応を起こさない」ことだけを見ています。実装上は、死者の投稿が `logs_public` に入ったあと、次の LLM 発言・投票・夜行動の `build_user_context()` に渡されます。Discord 権限で通常は防げますが、bot 側の defense-in-depth としては log insert も同じ alive participant gate に合わせるべきです。
- 推奨修正:
  `is_main and DAY_DISCUSSION` の分岐で、`author_is_living_player = _main_channel_should_llm_react(author_seat, players)` のように判定し、false の場合は public log への保存も LLM reaction も行わず return してください。必要なら非参加者/死者投稿を debug log にだけ残します。
- テストギャップ:
  死亡済み参加者のメインチャンネル投稿が `insert_log_public()` されないこと、後続 LLM prompt の public log に入らないことを確認する `on_message` integration test がありません。

### Low: LLM の `intent=skip` が非 null の `target_name` を伴うとランダム投票に変換される

- 影響:
  LLM が棄権意図で `intent="skip"` を返しても、`target_name` に `"棄権"` や空文字など non-null の不正値が入ると、現在の `_resolve_target()` は `allow_none=True` でもランダム候補を返します。秘密投票では棄権が許されているため、モデルの意図と逆にランダムな投票が保存される可能性があります。
- 根拠コード:
  - `src/wolfbot/llm/prompt_builder.py:454-463`
  - `src/wolfbot/services/llm_service.py:524-527`
  - `src/wolfbot/services/llm_service.py:777-796`
- 推奨修正:
  投票処理では `action.intent == "skip"` を先に判定し、`target_name` の内容に関係なく `target_seat=None` で `submit_vote()` してください。あるいは `_resolve_target(..., allow_none=True)` が解決不能な non-null 値を受けた場合は `None` を返す設計に変えます。ただし夜行動では `allow_none=False` のランダム fallback を維持して問題ありません。
- テストギャップ:
  `LLMAction(intent="skip", target_name="棄権")` がランダム候補ではなく `target_seat=None` を submit することを確認するテストがありません。

## 補足

- 前回までの主要指摘だった旧 SQLite schema 互換、狼襲撃 split の早期 wake、夜行動 `target_seat=None` の通常提出拒否は、現行コードとテストで対策済みでした。
- `render_pending_host_lines()` と `announce_waiting()` は role-identifying な未提出者名を公開しないようになっており、狼 split の詳細もメインチャンネルでは件数だけに抑えられています。
- `/wolf create` の stale private channel 削除は、DB に記録された bot 管理 channel ID のみに限定されており、手動作成の同名チャンネルを削除しない方針になっています。

## 追加推奨テスト

- 夜明け解決時の Discord 呼び出し順: private result -> morning -> phase/victory logs。
- 騎士が force-skip された夜の次夜に、2 夜前の護衛先が合法候補へ戻ること。
- 死亡プレイヤーのメインチャンネル投稿が public log に保存されず、LLM prompt に入らないこと。
- LLM 投票で `intent=skip` なら non-null の不正 `target_name` があっても棄権になること。
