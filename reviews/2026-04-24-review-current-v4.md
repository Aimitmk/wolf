# 人狼bot コードレビュー結果

## 概要

- 対象コミット: `532112a`
- 対象状態: 上記 `HEAD` の現在ワークツリー
- 対象: `src/wolfbot`, `tests`, `README.md`, `CLAUDE.md`
- 観点: 進行制御、Discord 権限/DM、LLM 非同期処理、再起動復旧、永続化、テストギャップ
- 結論: 自動チェックはすべて通過しています。前回までに指摘されていた旧 DB migration、夜行動の `None` target、狼 split の早期 wake は修正済みです。今回の主な残リスクは、Discord 実運用で表面化しやすい DM 事前確認、遅延 LLM タスク、メインチャンネル権限制御です。

## 実施した確認

- `uv run pytest tests -q` -> `355 passed, 1 warning in 2.21s`
- `uv run ruff check src tests` -> `All checks passed!`
- `uv run mypy` -> `Success: no issues found in 26 source files`

## 指摘事項

### Medium: `/wolf start` の DM 事前確認が実送信可否を保証していない

- 影響:
  `/wolf start` は人間参加者に対して `_preflight_dms()` を実行していますが、現在は `user.create_dm()` の成功だけで DM 可と判定しています。Discord では DM チャンネルを開けても、その後の `user.send(...)` がプライバシー設定などで `Forbidden` / `DiscordException` になるケースがあります。この場合、ゲーム開始後の役職通知、投票 UI、夜行動 UI が届かず、プレイヤーは提出不能になります。現行実装は送信失敗をログに残すだけで、開始後にゲームを止めたりホストへ明示通知したりしません。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:692-700`
  - `src/wolfbot/services/discord_service.py:859-875`
  - `src/wolfbot/services/discord_service.py:176-224`
  - `src/wolfbot/services/discord_service.py:226-287`
- 詳細:
  `_preflight_dms()` は開始前検査としてはよい位置にありますが、検査内容が「DM チャンネル作成」止まりです。一方、実際の秘密情報配布と UI 送信は `user.send(...)` で行われ、ここで失敗すると `send_private()` / `send_vote_dms()` / `send_night_action_dms()` は warning / exception log のみで進行自体は継続します。
- 推奨修正:
  `_preflight_dms()` で実際に短い確認 DM を `user.send(...)` し、送信失敗した参加者がいる場合は開始を拒否してください。空メッセージは送れないため、「人狼bot DM疎通確認」程度の短い文面にします。開始後の各 DM 送信失敗も、可能ならホストに公開チャンネルまたは ephemeral response で通知する導線を追加すると運用しやすくなります。
- 推奨テスト:
  `_preflight_dms()` の fake user が `create_dm()` は成功するが `send()` は失敗するケースを追加し、`/wolf start` が phase を `LOBBY` のまま維持して開始しないことを確認してください。

### Medium: LLM 狼チャットが LLM 応答後に phase/day を再確認せず、夜終了後に投稿されうる

- 影響:
  `_run_wolf_chat()` は各 LLM 狼について `_ask()` の前に `fresh.phase is Phase.NIGHT` と day を確認しています。しかし `_ask()` は外部 LLM 呼び出しで遅延しうるため、応答が返るまでに締切、force-skip、abort、勝利終了などでゲーム状態が変わる可能性があります。現在は `_ask()` 後に再ロードせず、古い `fresh` を使って wolves channel へ投稿し、`logs_private` に `WOLF_CHAT` を記録します。
- 根拠コード:
  - `src/wolfbot/services/llm_service.py:345-353`
  - `src/wolfbot/services/llm_service.py:366-386`
  - `src/wolfbot/services/llm_service.py:390-405`
- 詳細:
  同じファイルの昼発言 `_maybe_speak()` は LLM 呼び出し後、投稿直前に再度 `load_game()` して `DAY_DISCUSSION` と day を確認しています。夜行動の提出側 `_one_night_action()` も `GameService.submit_night_action()` 側で stale phase/day が拒否されます。一方で狼チャット投稿は `message_poster.post_wolves_chat()` と `insert_log_private()` に直接進むため、同等の stale guard がありません。
- 推奨修正:
  `_ask()` の直後、投稿前に `fresh = await self.repo.load_game(game.id)` を再実行し、`fresh is not None`、`fresh.ended_at is None`、`fresh.phase is Phase.NIGHT`、`fresh.day_number == game.day_number` を満たさない場合は return / continue してください。ログ記録もその再確認済み snapshot を使います。
- 推奨テスト:
  scripted decider を await 中に phase を `DAY_DISCUSSION` または `GAME_OVER` へ進めるテストを追加し、wolves channel 投稿も `WOLF_CHAT` private log も増えないことを確認してください。

### Low / Operational: メイン text の非参加者発言制御は bot 実装だけでは完結していない

- 影響:
  仕様上は「昼は生存者だけがメイン text に発言可能」ですが、`PermissionManager` は参加席に紐づくメンバー overwrite だけを更新します。観戦者、未参加メンバー、管理者など、席に存在しないユーザーの `send_messages` はメインチャンネルの基底権限に依存します。専用チャンネルの基底権限が開いている運用では、非参加者がメイン text に投稿でき、議論ログにも残りえます。
- 根拠コード:
  - `src/wolfbot/services/permission_manager.py:62-64`
  - `src/wolfbot/services/permission_manager.py:160-180`
  - `src/wolfbot/services/discord_service.py:442-492`
  - `README.md` のセットアップ手順では、メイン text の基底権限確認が運用前提として説明されています。
- 詳細:
  `on_message` 側では非参加者投稿を LLM 反応の入力から除外しており、LLM 操作リスクは抑えられています。ただし Discord 上の発言そのものを bot が防ぐわけではありません。実運用で「ゲーム中は参加者以外が発言できない」ことを期待するなら、現在の実装は README の運用手順に依存しています。
- 推奨修正:
  方針をどちらかに寄せるべきです。運用前提でよいなら README と `/wolf create` の失敗/注意メッセージをさらに明確にし、「メインチャンネルは事前に非参加者 send deny にする」と明記します。bot 側で閉じるなら、ゲーム中だけ `@everyone` の `send_messages=False` を設定し、参加者だけ per-member overwrite で許可する設計に変更します。ただし既存チャンネルを使うため、ゲーム終了時に元の overwrite を復元する設計が必要です。
- 推奨テスト:
  permission fake に `@everyone` overwrite を持たせ、ゲーム開始時に基底 send を閉じる設計を採る場合は、開始/終了で overwrite が期待通り更新・復元されることを確認してください。

## 修正済みであることを確認した前回指摘

- 旧 SQLite DB への additive migration:
  `games.force_skip_pending`、`seats.dm_channel_id`、`pending_decisions.submissions_json` は `PRAGMA table_info` guard 付きで追加され、旧 DB からの migration regression test もあります。
- 夜行動の `target_seat=None`:
  `GameService.submit_night_action()` で `None` が `SubmitResult.ILLEGAL_TARGET` として拒否されます。force-skip の「行動なし」は通常提出 API ではなく解決ロジック側に閉じています。
- 狼襲撃 split の早期 wake:
  `_all_night_actions_in()` が `resolve_wolf_attack(..., force_skip=False)` を使い、split 時は早期 wake しない実装になっています。締切後に `WAITING_HOST_DECISION` へ入る regression test も追加されています。

## 追加推奨テスト

- `/wolf start` の DM preflight で、`create_dm()` 成功かつ `send()` 失敗の参加者を検出して開始拒否すること。
- LLM 狼チャットで、LLM 応答待ち中に phase/day が変わった場合、wolves channel 投稿と private log 追加が行われないこと。
- メイン text の非参加者 send 権限を bot 側で管理する方針にする場合、開始時の `@everyone` deny、参加者 allow、ゲーム終了時の復元を permission-level test で確認すること。
