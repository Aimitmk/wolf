# 人狼bot コードレビュー結果

## 概要

- 対象コミット: `ec690ae` (`Show seat roster in /wolf start followup`)
- 対象: `src/wolfbot`, `tests`, `README.md`, `CLAUDE.md`
- 観点: 進行制御、ロビー競合、Discord 境界、LLM 発話、運用安全性
- 結論: 自動チェックはすべて通っていますが、ゲーム開始を破綻させる高優先度の競合不具合が 1 件、ゲーム整合性や運用安全性を崩す中優先度の問題が 3 件あります

## 実施した確認

- `uv run pytest tests -q` → `310 passed, 1 warning in 3.96s`
- `uv run ruff check src tests` → `All checks passed!`
- `uv run mypy` → `Success: no issues found in 26 source files`
- 追加で、`/wolf start` のロビー競合、`on_message` の LLM 反応経路、LLM cooldown 記録時刻を小さな再現スクリプトで確認しました

## 指摘事項

### High: `/wolf start` が `/wolf leave` と競合すると、8 席以下のまま `SETUP` に入りゲームが永久に進まなくなる

- 影響:
  開始直前に誰かが退出すると、`/wolf start` は古い人数スナップショットで不足 LLM 数を計算したまま `claim_start_and_backfill()` を実行します。その結果、9 席未満のままゲームが `SETUP` に遷移し、以後 `plan_setup()` が毎回 `ValueError` を投げて進行不能になります。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:668-687`
  - `src/wolfbot/persistence/sqlite_repo.py:790-831`
  - `src/wolfbot/domain/rules.py:21-27`
- 再現:
  8 人ロビーで `/wolf start` 側が `shortfall=1` を計算した直後に 1 人が `leave_lobby()` すると、`claim_start_and_backfill()` は成功しつつ座席数は 8 のまま残ります。手元再現でも `phase SETUP seat_count 8` になり、その直後の `GameService.advance()` は `ValueError: Village size must be 9; got 8` で落ちました。
- 詳細:
  `claim_start_and_backfill()` は「LOBBY であること」しか原子的に保証しておらず、「最終的に 9 席になること」は呼び出し側の事前計算に依存しています。`/wolf start` はトランザクション外で `shortfall` を決めてから DB claim するため、`leave_lobby()` が先に勝つと underfill を防げません。
- 推奨修正:
  LLM 補完数は `claim_start_and_backfill()` 内で現在の seat 数から再計算するべきです。少なくとも「現在の seat 数 + 渡された llm_seats 数 == 9」をトランザクション内で検証し、満たさなければ rollback する必要があります。
- テストギャップ:
  `tests/test_llm_backfill.py` は「同じ shortfall を前提にした start 競合」しか見ておらず、`/wolf leave` が shortfall 計算後に割り込むケースがありません。

### Medium: 非参加者のメインチャンネル投稿でも LLM が反応し、議論に介入させられる

- 影響:
  そのゲームに参加していないユーザーでも、メインチャンネルにキーワードを書くだけで LLM 発話を誘発できます。専用チャンネル運用を崩したときや管理者が発言できる設定のとき、観戦者や外部メンバーが村の議論を実質的に操作できます。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:448-474`
- 再現:
  `seat_of_user()` が `None` を返す非参加者メッセージでも、`on_message()` は `llm_adapter.maybe_react_to_message(..., author_seat=None, text=...)` を呼び出します。手元の簡易再現でも `await_count == 1` を確認しました。
- 詳細:
  メインテキスト側では `author_seat is not None` のときだけ公開ログに残しますが、LLM 反応は参加者チェックなしで常に走ります。人狼チャット側には「生存 wolf のみ」という防御が入っているのに、公開チャンネル側には同等の防御がありません。
- 推奨修正:
  少なくとも `author_seat is not None` かつ `author_player.alive` のときだけ LLM 反応対象にするべきです。観戦発言を許す運用でも、LLM への入力は参加プレイヤー発言のみに制限したほうが安全です。
- テストギャップ:
  `on_message()` 経由で「非参加者」または「死亡者」の発言が LLM 反応を起こさないことを確認するテストがありません。

### Medium: `/wolf create` は同名チャンネルを無条件に削除するため、手動作成の `wolf-heaven` / `wolf-wolves` を誤って消せる

- 影響:
  guild 内にたまたま同名の text channel があるだけで、`/wolf create` がそのチャンネルを stale 扱いで削除します。bot が過去に作ったチャンネルかどうかを確認していないため、運用者や他用途のチャンネル履歴を失うデータ消失リスクがあります。
- 根拠コード:
  - `src/wolfbot/services/discord_service.py:854-865`
- 詳細:
  `_create_private_channel()` は `discord.utils.get(guild.text_channels, name=name)` で最初に見つかった同名チャンネルを取得し、その由来を検証せず即 `delete()` しています。コメントでも「manual に作られていた場合」を purge 対象に含めています。
- 推奨修正:
  削除対象は「bot が作った前回ゲームの private channel」に限定するべきです。少なくとも DB 上の前回ゲーム ID や channel ID と照合する、category や topic に bot 管理印を残す、見つかっても削除せず明示的に失敗させる、のいずれかが必要です。
- テストギャップ:
  同名だが bot 管理外の既存チャンネルがあるケースの保護テストがありません。

### Medium: LLM の cooldown が「投稿時刻」ではなく「推論開始時刻」で記録されるため、遅い応答だと連投が通る

- 影響:
  xAI 応答が長引くと、実際の投稿直後にもかかわらず cooldown が切れた扱いになり、連続反応が許可されます。結果として、想定より短い間隔で LLM が連投し、議論ノイズや発話数制御の崩れを招きます。
- 根拠コード:
  - `src/wolfbot/services/llm_service.py:643-703`
- 再現:
  `decide()` 中に clock を 25 秒進める簡易再現では、1 回目投稿後の DB 状態が `clock=1025, last_spoke_epoch=1000` になりました。そのまま直後に 2 回目の反応を呼ぶと `count=2` まで進み、20 秒 cooldown をすり抜けました。
- 詳細:
  `_maybe_speak()` は LLM 呼び出し前に `now = self._clock()` を取得し、その値をログの `created_at` と `last_spoke_epoch` の両方に使っています。`post_public()` の時点では数秒から数十秒経っていても、cooldown 判定は古い時刻基準のままです。
- 推奨修正:
  `post_public()` 成功後に改めて現在時刻を取り直し、その時刻を `created_at` と `last_spoke_epoch` に使うべきです。少なくとも cooldown の起点は「実際に投稿した時刻」に揃える必要があります。
- テストギャップ:
  LLM 推論に時間がかかったケースで cooldown が投稿完了基準になることを確認するテストがありません。

## 補足

- 既存レビューで指摘されていた `/wolf create` 同時実行、片側チャンネル作成失敗 cleanup、LLM 投稿失敗時の speech count 消費は、現行コードでは解消済みでした。
- 今回の High は「コマンド単体の競合」ではなく、「`/wolf start` の事前人数スナップショット」と「原子的な phase claim」の責務分離が崩れていることが原因です。`join_lobby` / `leave_lobby` の原子化だけでは塞げません。

## 追加推奨テスト

- `/wolf start` が人数スナップショット取得後に `leave_lobby()` と競合しても、`SETUP` に入る前に 9 席 invariant を保証すること
- `on_message()` が非参加者メッセージ、死亡者メッセージで `maybe_react_to_message()` を呼ばないこと
- `_create_private_channel()` が bot 管理外の同名 channel を削除しないこと
- LLM の `last_spoke_epoch` が推論開始ではなく投稿完了時刻で更新されること
