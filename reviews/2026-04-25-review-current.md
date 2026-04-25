# 人狼bot コードレビュー結果

## 概要

- 対象状態: 現在のワークツリー (`git status --short` は出力なし)
- 対象: `src/wolfbot`, `tests`, `prompts/IMPLEMENTATION_PROMPT.md`, LLM prompt 周辺
- 観点: 状態遷移、夜行動、復旧、永続化、Discord/LLM 境界、運用安全性
- 結論: 自動チェックはすべて通過しています。既存レビューで指摘されていた DB migration、夜行動 `None` target、狼 split の早期停止は修正済みです。一方で、human/LLM 混合狼に追加された人間優先ルールと、早期 wake 判定・LLM ルール説明の間に不整合が残っています。

## 実施した確認

- `uv run pytest tests` -> `574 passed, 1 warning in 4.59s`
- `uv run ruff check src tests` -> `All checks passed!`
- `uv run mypy` -> `Success: no issues found in 26 source files`
- 警告は `discord/player.py` 経由の Python 3.13 向け `audioop` deprecation で、現行の Python 3.11 実行には直接影響しません。

## 指摘事項

### Medium: human/LLM 混合狼の襲撃は解決済みなのに、締切まで早期進行しない

- 影響:
  生存人狼 2 名のうち片方が人間、片方が LLM で、別々の襲撃先を提出した場合、夜行動の解決側では「人間狼の選択を採用する」ため結果は確定しています。しかし早期 wake 判定側はその人間優先情報を渡していないため split と判定し、全必要行動が揃っていても `wake()` しません。結果として、夜が最大 `NIGHT_DURATION` の 90 秒ぶん不要に待機します。
- 根拠コード:
  - `src/wolfbot/domain/rules.py:153-184`
  - `src/wolfbot/domain/state_machine.py:586-600`
  - `src/wolfbot/services/game_service.py:832-858`
- 詳細:
  `plan_night_resolve()` は `human_wolf_seats` を計算して `resolve_wolf_attack(..., human_wolf_seats=...)` に渡しています。一方、`GameService._all_night_actions_in()` は同じ `resolve_wolf_attack()` を呼ぶものの `human_wolf_seats` を渡さないため、混合狼の不一致も `attack.split=True` になります。
- 推奨修正:
  `_all_night_actions_in()` でも seats を読み込み、人間狼 seat を `resolve_wolf_attack()` に渡してください。可能なら「現在の狼襲撃が早期解決可能か」を共通 helper 化し、`plan_night_resolve()` と早期 wake 判定が同じ入力で同じ結論を出す形に寄せるのが安全です。
- 追加テスト:
  人間狼 + LLM 狼が別ターゲットを提出し、占い・護衛も提出済みなら早期 wake されること。あわせて、両方人間または両方 LLM の split は引き続き早期 wake されず、締切後に `WAITING_HOST_DECISION` へ入ることを固定してください。

### Medium: LLM prompt の狼襲撃ルール説明が実装の human-wolf priority と一致していない

- 影響:
  LLM には「人狼同士で夜の襲撃対象の意見が割れると襲撃は空振りになる」と説明されていますが、実装上は human/LLM 混合狼で意見が割れた場合、人間狼の選択が採用されます。LLM 狼は実際の解決ルールと違う前提で人狼チャットや襲撃対象を選ぶため、特に人間相方がいるゲームで行動品質が落ちます。
- 根拠コード:
  - `src/wolfbot/llm/prompt_builder.py:65-66`
  - `src/wolfbot/domain/rules.py:153-184`
  - `src/wolfbot/domain/state_machine.py:586-600`
- 詳細:
  実装コメントでは human-wolf priority が明示されていますが、仕様文 `prompts/IMPLEMENTATION_PROMPT.md` は「1 対 1 で割れた場合、締切時点では未確定」としており、LLM prompt もその仕様に沿った説明のままです。現状は「実装だけが例外ルールを持つ」状態です。
- 推奨修正:
  human-wolf priority を正式ルールとして残すなら、LLM prompt と人間向け仕様にも同じ例外を明記してください。仕様に合わせるなら、priority ルールを削除し、全ての 1 対 1 split を `WAITING_HOST_DECISION` へ寄せるべきです。どちらにしても、ドメインルール・prompt・仕様文の 3 箇所を同じルールに揃える必要があります。
- 追加テスト:
  `build_system_prompt()` の共有ルール block が採用した正式ルールを含むこと、または priority 削除後に混合狼 split も `AttackResult.split=True` になることをテストしてください。

### Low: `/wolf create` 中のプロセス停止で orphan private channel が残ると、次回 create が手動復旧待ちになり得る

- 影響:
  `/wolf create` は `wolf-heaven` と `wolf-wolves` を Discord 上に作成してから `games` 行を作成します。2 つのチャンネル作成後、`repo.create_game()` より前にプロセスが落ちると、チャンネル ID が DB に残りません。次回 `/wolf create` では `safe_to_delete_ids` に含まれない同名チャンネルとして扱われ、bot は安全のため削除を拒否します。秘密漏洩を防ぐ設計としては妥当ですが、運用上は手動削除が必要になります。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:532-572`
  - `src/wolfbot/services/discord_service.py:867-914`
- 詳細:
  `ActiveGameExistsError` では作成済みチャンネルを削除していますが、プロセス停止や広い `create_game()` 例外には対応できません。特に DB 書き込み前の crash はアプリ側の recovery からは orphan channel と手動作成チャンネルを区別できません。
- 推奨修正:
  運用許容なら README/運用メモに「create 中断後に同名 channel が残った場合は手動削除」と明記してください。自動復旧したい場合は、ゲーム ID を含む一意な channel 名にする、または channel 作成直後に pending 状態を DB に記録できる作成フローへ変更し、次回起動時に bot 管理 channel として識別できるようにしてください。

## 修正済みと判断した既存指摘

- 旧 SQLite DB 向け additive migration は、`games.force_skip_pending`、`seats.dm_channel_id`、`pending_decisions.submissions_json`、`llm_speech_counts` 追加列の guard が入っています。
- `submit_night_action(..., target_seat=None)` は通常夜行動として拒否され、保存されないようになっています。
- 両狼が同種 seat 構成で別ターゲットを提出した場合、締切前には早期 wake せず、締切後に `WAITING_HOST_DECISION` へ入る regression test が追加されています。
- DAY_VOTE / DAY_RUNOFF は締切前の部分提出で `WAITING_HOST_DECISION` へ入らない guard が入っています。
- 非参加者・死亡者のメインチャンネル投稿は LLM 入力ログに入らず、LLM 反応も起こさない gate になっています。

## 追加推奨テスト

- human/LLM 混合狼が別ターゲットを提出したとき、正式採用するルールどおりに早期 wake または split 待機すること。
- LLM prompt の共有ルール block が、ドメインの狼襲撃解決ルールと矛盾しないこと。
- `/wolf create` の channel 作成後に `repo.create_game()` が一般例外を投げた場合、可能な範囲で作成済み channel を cleanup すること。プロセス停止ケースは自動テストしにくいため、少なくとも通常例外 path の cleanup を固定すると運用事故を減らせます。
