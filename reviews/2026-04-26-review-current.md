# 人狼bot コードレビュー結果

## 概要

- 対象状態: 現在のワークツリー。レビュー開始時点の `git status --short` は出力なし。
- 対象: `src/wolfbot`, `tests`, `.env.example`, `README.md`, `CLAUDE.md`, prompt 周辺。
- 観点: 状態遷移、復旧、タイマー、夜行動、Discord/LLM 境界、永続化、運用安全性、テスト不足。
- 結論: 自動チェックはすべて通過しています。直近レビューで指摘されていた human/LLM 混合狼の早期 wake、LLM 共有ルール説明、`/wolf create` 通常例外時 cleanup は修正済みです。一方で、復旧時の LOBBY engine 起動による busy loop と、復旧用 pending 判定・一部 prompt 文言の human-wolf priority 不整合が残っています。

## 実施した確認

- `uv run pytest tests` -> `593 passed, 1 warning in 8.41s`
- `uv run ruff check src tests` -> `All checks passed!`
- `uv run mypy` -> `Success: no issues found in 26 source files`
- 警告は `.venv/lib/python3.11/site-packages/discord/player.py` 経由の Python 3.13 向け `audioop` deprecation で、現行の Python 3.11 実行には直接影響しません。

## 指摘事項

### High: 再起動時に LOBBY のゲームへ GameEngine を attach すると busy loop する

- 影響:
  `/wolf create` 済み、`/wolf start` 前の LOBBY 状態で bot を再起動すると、復旧処理がその LOBBY game にも `GameEngine` を attach します。`GameEngine` は `deadline_epoch=None` を transient phase として即時 `advance()` しますが、`GameService.advance()` は LOBBY を no-op で返すため phase も deadline も変わりません。結果として engine が sleep なしで `load_game()` と `advance()` を繰り返し、CPU と DB を無駄に消費し続けます。
- 根拠コード:
  - `src/wolfbot/services/recovery_service.py:57-66`
  - `src/wolfbot/services/recovery_service.py:105-116`
  - `src/wolfbot/services/timer_service.py:88-97`
  - `src/wolfbot/services/game_service.py:176`
- 詳細:
  `RecoveryService.recover_all()` は `ended_at IS NULL` の全 game を対象にし、phase を絞らず `_recover_one()` で engine を作って起動します。LOBBY は `deadline_epoch=None` ですが、`GameEngine._run()` 側では LOBBY を待機 phase として扱っていません。
- 推奨修正:
  復旧時は LOBBY game には engine を attach しない、または `GameEngine` 側で LOBBY を `WAITING_HOST_DECISION` と同様に wake 待ち扱いにしてください。さらに `advance()` が進捗なしで返った場合に短い backoff を入れる、または `advance()` を bool 返却にして no-progress を engine が判定できる形にすると、権限適用失敗時の同種 busy loop も防げます。
- 追加テスト:
  LOBBY の active game を復旧しても registry に engine が増えない、または engine が `advance()` を連続呼び出ししないこと。あわせて、SETUP など本当に transient な phase は従来どおり自動進行することを固定してください。

### Medium: 復旧用 pending 判定だけ human/LLM 混合狼の襲撃 priority を見ていない

- 影響:
  生存人狼 2 名のうち片方が人間、片方が LLM の構成で襲撃先が割れた場合、通常の夜解決と早期 wake 判定は人間狼の選択を採用します。しかし `submission_snapshot.unresolved_submitters()` は seat 情報を持たず `resolve_wolf_attack()` に `human_wolf_seats` を渡していないため、復旧・延長用の pending 判定では同じ状況を未確定 split と扱います。bot 再起動や `/wolf extend` 経路で、解決済み扱いできる狼に不要な再提出 DM が飛ぶ可能性があります。
- 根拠コード:
  - `src/wolfbot/services/submission_snapshot.py:86-105`
  - `src/wolfbot/domain/state_machine.py:586-600`
  - `src/wolfbot/services/game_service.py:832-865`
  - `src/wolfbot/domain/rules.py:153-184`
- 詳細:
  `state_machine.plan_night_resolve()` と `GameService._all_night_actions_in()` は seats から人間狼 seat を計算して `resolve_wolf_attack(..., human_wolf_seats=...)` に渡しています。一方、`derive_pending()` が使う `unresolved_submitters()` は同じ helper を呼びながら human-wolf priority の入力だけ欠けています。
- 推奨修正:
  `unresolved_submitters()` に `seats` を渡す、または「現在の狼襲撃が未確定か」を seats 込みの共通 helper に切り出して、通常解決・早期 wake・復旧 pending の 3 経路が同じ判定を使うようにしてください。
- 追加テスト:
  人間狼 + LLM 狼が別ターゲットを提出済みで、他の夜行動も提出済みの NIGHT を復旧したとき、`PendingSubmission.unresolved_seats` に両狼が入らないこと。双方人間 / 双方 LLM の split は引き続き unresolved になること。

### Low: 狼の夜行動 task 文言が human-wolf priority 例外をまだ省略している

- 影響:
  共有ルール block と狼 role strategy には human/LLM 混合狼では人間の襲撃先が採用される例外が追加済みですが、夜行動用 task 文言では「意見が割れると襲撃が空振りになります」とだけ書かれています。LLM は同一 system prompt 内で少し違う説明を読むため、混合狼の実ルール認識がぶれます。
- 根拠コード:
  - `src/wolfbot/llm/prompt_builder.py:65-69`
  - `src/wolfbot/llm/prompt_builder.py:257-261`
  - `src/wolfbot/llm/prompt_builder.py:813-817`
- 推奨修正:
  `task_night_action()` の狼向け追加文にも「原則 split は失敗。ただし人間 + LLM の 2 狼構成では人間の選択が採用される」と明記してください。
- 追加テスト:
  `task_night_action(SubmissionType.WOLF_ATTACK, ...)` の文面が human-wolf priority 例外を含むことを固定してください。

### Low: xAI model の既定値説明がファイル間で食い違っている

- 影響:
  実装とテスト上の `Settings.XAI_MODEL` 既定値は `grok-4-1-fast` ですが、`.env.example`、README の最小設定例、`CLAUDE.md` では `grok-4-1-fast-reasoning` が既定または推奨値のように見えます。`.env.example` をコピーした運用者は実装の既定値とは違うモデルで起動します。
- 根拠コード:
  - `src/wolfbot/config.py:26-28`
  - `tests/test_config.py:30-33`
  - `.env.example:11-13`
  - `README.md:44-46`
  - `README.md:120-123`
  - `CLAUDE.md:39-40`
- 推奨修正:
  `grok-4-1-fast` と `grok-4-1-fast-reasoning` のどちらを正式な既定値にするか決め、`Settings`、テスト、`.env.example`、README、`CLAUDE.md` を同じ値に揃えてください。

## 修正済みと判断した既存指摘

- human/LLM 混合狼が別ターゲットを提出した場合、通常解決では人間狼の選択が採用されます。
- `_all_night_actions_in()` も human-wolf priority を反映しており、混合狼の襲撃が解決済みなら早期 wake されます。
- LLM 共有ルール block と狼 role strategy には human-wolf priority の説明が追加済みです。
- `/wolf create` は `repo.create_game()` の通常例外時に作成済み private channel を cleanup します。
- SQLite の additive migration、`target_seat=None` の夜行動拒否、両方人間/両方 LLM の狼 split 待機、DAY_VOTE / DAY_RUNOFF の締切前 WAITING 防止、死亡者/非参加者の PLAYER_SPEECH gate は regression test 付きで維持されています。

## 追加推奨テスト

- LOBBY 状態の active game を recovery しても engine が busy loop しないこと。
- permission application failure で phase が進まない場合でも engine が sleep なしで再試行し続けないこと。
- 復旧用 `derive_pending()` / `unresolved_submitters()` が human-wolf priority と通常夜解決の判定に一致すること。
- `task_night_action()` の狼向け文言が domain の襲撃解決ルールと矛盾しないこと。
- `XAI_MODEL` の正式既定値が config、tests、`.env.example`、README、`CLAUDE.md` で一致すること。
