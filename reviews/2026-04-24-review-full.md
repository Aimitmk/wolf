# 人狼bot コードレビュー結果

## 概要

- 対象: `src/wolfbot`, `tests`, `README.md`, `CLAUDE.md`
- 観点: 進行制御、Discord 境界、復旧、LLM 発話、運用安全性
- 結論: 自動チェックはすべて通過していますが、実運用で破綻しうる高優先度の問題が 1 件、運用上の不整合や後続障害につながる中優先度の問題が 2 件あります

## 実施した確認

- `uv run pytest tests -q` → `303 passed, 1 warning in 3.34s`
- `uv run ruff check src tests` → `All checks passed!`
- `uv run mypy` → `Success: no issues found in 26 source files`
- 追加で、`/wolf create`、復旧、LLM 発話の主要経路を静的に追い、既存レビューで指摘されていた論点が現行コードで解消済みかも確認しました

## 指摘事項

### High: `/wolf create` の競合で、勝者のゲームが削除済み秘密チャンネル ID を保持しうる

- 影響:
  `wolf-heaven` / `wolf-wolves` のチャンネル ID が、作成直後に別リクエストから削除されたチャンネルを指しうります。そうなると以後の権限同期、秘密会話、復旧通知は DB 上のチャンネル ID を参照しても対象チャンネルを取得できず、秘密チャンネル機能が壊れます。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:516-547`
  - `src/wolfbot/services/discord_service.py:818-839`
- 詳細:
  `/wolf create` は DB にゲームを確定する前に秘密チャンネルを 2 本作成します。一方 `_create_private_channel()` は、同名チャンネルが存在すると「前回の残骸」とみなして即削除します。この 2 つが組み合わさると、同一 guild で `/wolf create` が競合したとき、後から走ったリクエストが先行リクエストの freshly created channel を stale 扱いで消し、その後の `create_game()` 競合で敗者になっても cleanup で自分が作ったチャンネルを消すため、勝者側の `Game.heaven_channel_id` / `wolves_channel_id` がすでに削除済みの ID になるパスが成立します。
- 成立条件:
  同一 guild で `/wolf create` がほぼ同時に 2 回走ること。既存の active game 一意制約自体は効いていますが、チャンネル生成が DB claim より先なので防げていません。
- 推奨修正:
  先に DB 側で「この guild の create 権を取った」ことを確定してからチャンネルを作るべきです。少なくとも `_create_private_channel()` の stale-channel purge を、同時実行中の新規作成チャンネルまで消しうる設計のまま使うべきではありません。
- テストギャップ:
  `/wolf create` の競合を直接検証する回帰テストがありません。

### Medium: `/wolf create` で片側チャンネル作成だけ成功した場合、孤立した秘密チャンネルが残る

- 影響:
  `wolf-heaven` または `wolf-wolves` の片方だけが作成されたままゲーム作成が中断され、次回の `/wolf create` 時に「前回の残骸」として削除されるまで孤立チャンネルが残ります。運用上はノイズであり、障害調査時の誤誘導要因にもなります。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:516-521`
- 詳細:
  `heaven = await ...` と `wolves = await ...` を両方実行したあとで `if heaven is None or wolves is None:` を判定しているため、片方だけ成功したケースでも cleanup なしで `return` します。たとえば `heaven` 作成成功後に `wolves` 作成が失敗すると、`heaven` は DB に紐付かないまま残ります。
- 成立条件:
  片側だけ Discord API 失敗、権限不足、または stale-channel purge 失敗が起きること。
- 推奨修正:
  片側だけ作成できた時点で全体失敗にするなら、成功済みチャンネルを必ず rollback delete するべきです。
- テストギャップ:
  片側作成成功 / 片側失敗の cleanup を確認するテストがありません。

### Medium: LLM 発話の Discord 投稿に失敗しても、発話回数と cooldown だけ消費される

- 影響:
  一時的な Discord 送信失敗で実際には 1 文字も投稿されていないのに、その LLM は「発話済み」として扱われます。結果として、その日の日次 cap を無駄に消費し、以後の反応発話や daystart 発話が抑止されます。
- 根拠コード:
  - `src/wolfbot/services/llm_service.py:643-703`
- 詳細:
  `_maybe_speak()` は `post_public()` 成功時だけ公開ログを書いていますが、`increment_llm_normal_speech()` は `posted` 判定の外側で常に実行しています。つまり `post_public()` が例外を投げて `posted=False` のままでも、speech count と `last_spoke_epoch` が進みます。
- 成立条件:
  Discord API の一時失敗、権限ミス、接続不安定などで `message_poster.post_public()` が失敗すること。
- 推奨修正:
  少なくとも count / cooldown の更新は「実際に投稿できたとき」に限定するべきです。失敗時に再試行したい設計なら、公開ログ挿入と count 更新は同じ成功条件に揃える必要があります。
- テストギャップ:
  LLM 発話の送信失敗時に `llm_speech_counts` が増えないことを確認するテストがありません。

## 補足

- 既存レビューで問題だった以下の論点は、現行コードでは解消済みと判断しました。
  - guild ごとの active game 一意性
  - reconnect / `on_ready` 再発火時の engine 多重起動
  - recovery 時の pending 復元精度
  - `PermissionManager` の差分適用
- 今回の指摘はいずれも通常フローでは見えにくく、同時実行、部分失敗、外部 API 失敗時に表面化する運用系の欠陥です。

## 追加推奨テスト

- `/wolf create` 競合時に、勝者の `heaven_channel_id` / `wolves_channel_id` が実在チャンネルを指し続けること
- `/wolf create` で片側チャンネル作成だけ成功した場合に、成功済みチャンネルが cleanup されること
- LLM 発話の `post_public()` 失敗時に `llm_speech_counts` が更新されないこと
