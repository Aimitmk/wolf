# `wolfbot` 2026-04-25 ターン制昼議論・投票/襲撃フロー更新プロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の昼議論、投票結果表示、決選投票、夜の人狼襲撃フローを更新する。
- LLM player がいる村でも、公開情報・秘匿情報・進行タイミングが破綻しないようにする。
- 主眼はゲーム進行の自然さと秘匿性の維持であり、無関係な仕様変更はしない。

今回必ず対応すること:
1. 昼議論をターン性人狼に近づけ、LLM player は昼に 2 巡発言する。呼びかけられた場合に追加で話すリアクション機能は不要。
2. 昼議論から投票への遷移は「制限時間が経過した」かつ「LLM の昼 2 巡処理が完了した」の両方を満たした後に行う。つまり実質的に長い方を待つ。
3. 投票結果表示を「投票された人基準の集計」ではなく「投票する人基準の一覧」にする。
4. 通常投票が同票で、決選候補に LLM player が含まれる場合は、その候補 LLM だけが発言してから決選投票へ進む。
5. 人間 player と LLM player が人狼で、夜の襲撃先が食い違った場合は、人間 player の襲撃先を優先する。昼投票にはこの優先ルールを適用しない。
6. 人狼の噛み先不一致や未確定状態が全体に表示されることで、人狼が 2 人残っていると推測できないようにする。処理は DM と LLM 再ディスパッチで行う。

固定する解釈:
- 「LLM player の発言が 2 巡する or 制限時間の長い方」は、LLM 2 巡完了と制限時間経過の両方を満たすまで待つ、という意味で実装する。
- 「決選投票時に LLM player が含まれる」は、通常投票の同票候補に LLM seat が含まれる場合を指す。
- 「人間と LLM player が人狼の場合は人間の投票先を優先」は、夜の襲撃先の優先を指す。DAY_VOTE / DAY_RUNOFF の票は改変しない。
- LLM の発言生成が API 失敗・skip・空文字で終わった場合でもゲームを永遠に止めない。発言試行は完了扱いにして進行可能にする。ただし成功した発言だけを公開ログに残す。

最重要ルール:
- まず既存実装を読み、現在の advance loop / LLM fire-and-forget / recovery / pending decision / Discord notification の流れを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つ。Discord I/O、LLM I/O、DB I/O を domain に入れないこと。
- 既存の optimistic lock (`SqliteRepo.apply_transition(..., expected_phase=...)`) を迂回しないこと。
- LLM の xAI 呼び出しを `GameService.advance()` の通常パスで長時間 await しないこと。昼 2 巡や決選前発言も fire-and-forget + persisted progress + wake で扱うこと。
- Discord channel history を直接 prompt に入れないこと。LLM 文脈は既存どおり DB の public/private logs から構築すること。
- 秘密情報を main text channel、公開 status、recovery announcement に漏らさないこと。
- slash command は追加しないこと。
- 9 人村固定配役や勝利条件を変えないこと。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/domain/enums.py`
- `src/wolfbot/domain/models.py`
- `src/wolfbot/domain/rules.py`
- `src/wolfbot/domain/state_machine.py`
- `src/wolfbot/services/game_service.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/services/discord_service.py`
- `src/wolfbot/services/timer_service.py`
- `src/wolfbot/services/submission_snapshot.py`
- `src/wolfbot/services/recovery_service.py`
- `src/wolfbot/persistence/schema.py`
- `src/wolfbot/persistence/sqlite_repo.py`
- `tests/test_state_machine_votes.py`
- `tests/test_state_machine_nights.py`
- `tests/test_game_service_advance.py`
- `tests/test_llm_trigger.py`
- `tests/test_discord_service.py`
- `tests/test_wolf_status_rendering.py`
- `tests/test_recovery.py`
- `tests/fakes.py`

このリポジトリで確認済みの事実:
- `GameEngine` は `deadline_epoch` または `wake()` で `GameService.advance(game_id)` を呼ぶ。
- `GameService.advance()` は現在 phase を読み、`domain.state_machine.plan_*()` で `Transition` を作り、権限更新、DB commit、Discord 通知、DM / LLM ディスパッチ、wake の順で進める。
- 現在の `DAY_DISCUSSION` は `plan_day_discussion_to_vote()` が単純に `DAY_VOTE` へ進める。
- 現在の `LLMAdapter.submit_llm_daystart_speeches()` は昼開始時に LLM を順番に 1 回ずつ話させる。
- 現在の `LLMAdapter.maybe_react_to_message()` は main text の人間発言に対して、名前・キーワード・クールダウン・発言上限を見て LLM を反応発言させる。
- 現在の `WolfCog.on_message()` は main text の人間発言を `PLAYER_SPEECH` として public log に保存した後、`maybe_react_to_message()` を呼んでいる。
- 現在の投票結果表示は `_format_vote_tally()` で「投票された人 -> 投票者一覧」形式に集計している。
- 現在の通常投票同票は `DAY_VOTE -> DAY_RUNOFF` へ直接遷移し、その場で決選投票 DM / LLM 決選投票がディスパッチされる。
- 現在の人狼襲撃は `resolve_wolf_attack()` が 2 狼一致なら襲撃、食い違いなら split として扱う。
- 現在の split wolf attack は `unresolved_seats` として表現され、`resend_pending_dms()` が再 DM / LLM 再ディスパッチする仕組みを持っている。
- 現在の `announce_waiting()` と `/wolf status` 系表示は role-identifying kind の名前は隠しているが、`WOLF_ATTACK` や件数が見えるため、今回の要件ではさらに秘匿する必要がある。

実装要求

## 1. 昼議論を LLM 2 巡 + 制限時間の両方待ちにする

必要な仕様:
- `DAY_DISCUSSION` に入ったら、生存 LLM player が seat 順で 2 巡発言する。
- 1 巡とは、その時点の生存 LLM player 全員に 1 回ずつ発言機会を与えること。
- LLM がいない場合は LLM 2 巡条件を即完了扱いにする。
- 昼議論から `DAY_VOTE` へ進む条件は以下の両方:
  - `now >= game.deadline_epoch`
  - 生存 LLM player の昼 2 巡処理が完了している
- LLM 2 巡が制限時間より先に終わった場合、制限時間までは `DAY_DISCUSSION` のまま待つ。
- 制限時間を過ぎても LLM 2 巡が終わっていない場合、LLM 2 巡完了まで `DAY_DISCUSSION` のまま待つ。
- API 失敗、skip、空文字でも、その LLM のその round は完了扱いにして、ゲームが止まり続けないようにする。

実装方針:
- `LLMAdapter.submit_llm_daystart_speeches()` を置き換えるか、互換 wrapper を残して `submit_llm_discussion_rounds()` 相当に整理する。
- 昼発言 round の進捗は DB に永続化する。既存の `llm_speech_counts` に additive column を足すか、同等の小さな進捗テーブルを追加する。
- 推奨は `llm_speech_counts` に `discussion_rounds_done INTEGER NOT NULL DEFAULT 0` を追加し、`game_id/day/seat_no` ごとに 0..2 を保存すること。
- `schema.py` の `CREATE TABLE` と `migrate()` の additive migration の両方を更新すること。
- `SqliteRepo` に、少なくとも以下相当の helper を追加すること:
  - seat ごとの昼 round 完了数を読む
  - seat ごとの昼 round 完了数を monotonic に更新する
  - current day の alive LLM seats が 2 round 完了しているか判定するための材料を返す
- `LLMAdapter` の昼 2 巡 task は background task とし、seat ごと・round ごとに stale phase/day check を行うこと。
- 各 LLM 発言成功時は従来どおり `post_public(..., kind="LLM_SPEAK")` と `logs_public` への `PLAYER_SPEECH` 保存を行う。
- 各 LLM round の完了は、発言成功/skip/失敗に関係なく `finally` 的に保存する。
- 全 LLM 2 巡完了後は `GameService` / `WakeSink` 経由で engine を wake する。
- `GameService._plan_next()` の `DAY_DISCUSSION` 分岐は、deadline 到達と LLM 2 巡完了の両方を確認してから `plan_day_discussion_to_vote()` を呼ぶ。
- deadline 到達済みだが LLM 2 巡未完了の場合は、同じ `DAY_DISCUSSION` に留まる no-log transition を commit し、短い再チェック deadline を設定するか、LLM 完了 wake で進める構造にする。engine が deadline 超過で tight loop しないようにすること。
- same-phase transition で `DAY_DISCUSSION` に留まる場合、LLM 2 巡 task を重複ディスパッチしないこと。`_dispatch_submissions()` には previous phase を渡すか、明示的な transition instruction を使うこと。
- recovery 時に `DAY_DISCUSSION` で未完了の LLM round がある場合は再ディスパッチする。完了済み round は再実行しない。

呼びかけ反応機能の削除:
- `WolfCog.on_message()` は main text の人間発言を引き続き `PLAYER_SPEECH` として保存する。
- ただし `LLMAdapter.maybe_react_to_message()` は呼ばない。
- `LLMAdapter.maybe_react_to_message()`、`_is_triggered()`、`REACTION_KEYWORDS`、reactive 用 cooldown/cap は削除してよい。互換性のため残す場合でも no-op にし、runtime からは使わないこと。
- 既存の `tests/test_llm_trigger.py` は、新仕様の昼 2 巡テストへ置き換えること。旧リアクション挙動を期待するテストは削除または新仕様に更新すること。

## 2. 投票結果を投票者基準に変更する

必要な仕様:
- 投票結果ログは「誰が誰に投票したか」を投票者 seat 順に表示する。
- 例: `・P1 -> P7`
- 棄権は `・P8 -> 棄権` のように表示する。
- 見出しは既存どおり `🗳 投票結果:` を使ってよい。
- 処刑ログ、決選開始ログ、処刑なしログの投票結果ブロックすべてを新形式にする。

実装方針:
- `state_machine.py` の `_format_vote_tally()` を `_format_vote_results_by_voter()` 相当に置き換える。
- alive seat 以外の stale vote は今までどおり表示対象から外す。
- `target_seat=None` は棄権として表示する。
- self-vote や dead target は service 層で reject される前提を維持し、表示 formatter 側で新しいルール処理を増やしすぎないこと。
- 集計結果そのもの (`compute_vote_result`) は変更しないこと。
- `tests/test_state_machine_votes.py` の投票結果テストを新形式に更新すること。

## 3. 決選候補に LLM がいる場合、発言してから決選投票へ進む

必要な仕様:
- 通常投票で同票が発生したとき、同票候補に生存 LLM seat が 1 人以上含まれる場合は、すぐ `DAY_RUNOFF` に入らない。
- まず同票候補 LLM だけが 1 回ずつ公開発言する。
- 発言対象は候補 LLM のみ。候補ではない LLM はこの決選前発言をしない。
- 人間候補には bot は発言待ちをしない。
- 候補 LLM の発言試行が完了してから、`DAY_RUNOFF` に進め、決選投票 DM と LLM 決選投票を開始する。
- 同票候補に LLM がいない場合は、従来どおり直接 `DAY_RUNOFF` へ進む。

実装方針:
- 推奨は `Phase.DAY_RUNOFF_SPEECH` を追加すること。
- `DAY_VOTE` 解決時:
  - tie あり、かつ tied candidates に LLM が含まれる場合: `DAY_RUNOFF_SPEECH` へ遷移する。
  - tie あり、かつ tied candidates に LLM が含まれない場合: 従来どおり `DAY_RUNOFF` へ遷移する。
- `DAY_RUNOFF_SPEECH` は、候補 LLM 発言が完了したら `DAY_RUNOFF` へ進む中間 phase とする。
- `DAY_RUNOFF_SPEECH` の進捗も DB に永続化する。推奨は `llm_speech_counts` に `runoff_speech_done INTEGER NOT NULL DEFAULT 0` を追加すること。
- `LLMAdapter.submit_llm_runoff_candidate_speeches()` 相当を追加し、候補 LLM だけを background task で発言させる。
- 各候補 LLM の発言試行後、成功/skip/失敗に関係なく `runoff_speech_done=1` 相当を保存する。
- 全候補 LLM の発言試行完了後、engine を wake する。
- `GameService._plan_next()` に `DAY_RUNOFF_SPEECH` 分岐を追加し、通常投票 round 0 から tied candidates を再計算して、対象 LLM の発言完了後に `DAY_RUNOFF` へ進める。
- `DAY_RUNOFF` に入るタイミングで初めて `send_vote_dms(..., round_=1)` と `submit_llm_votes(..., round_=1)` を実行する。
- recovery 時に `DAY_RUNOFF_SPEECH` で止まっていた場合は、未完了の候補 LLM 発言を再ディスパッチする。

注意:
- `DAY_RUNOFF_SPEECH` は人間の入力待ち phase ではないため、`WAITING_HOST_DECISION` の通常 pending submission と混ぜない。
- ただし API 失敗で永遠に止めないため、発言 task 側は失敗時も progress を保存する。
- 決選候補リストは round 0 vote result から常に再計算し、DB に重複保存しない方針でよい。

## 4. 夜の襲撃先は人間狼を優先する

必要な仕様:
- 生存人狼が 2 人で、片方が人間 player、片方が LLM player の場合に限り、襲撃先が食い違ったら人間 player の合法な襲撃先を採用する。
- この場合は split として `WAITING_HOST_DECISION` に入れない。
- 2 人とも人間、または 2 人とも LLM で襲撃先が割れた場合は、従来の split / unresolved 処理を使う。
- 人間狼が未提出で LLM 狼だけが提出した場合は、人間優先で勝手に LLM 先を採用しない。未提出として扱う。
- `force_skip=True` の挙動は既存設計を尊重し、未提出や split の扱いを無関係に変えない。
- 昼投票 (`DAY_VOTE`, `DAY_RUNOFF`) は人間狼に合わせて票を改変しない。

実装方針:
- `resolve_wolf_attack()` に `human_wolf_seats` のような引数を追加するか、state machine 側で同等の純粋 helper を使う。
- domain には Discord I/O を入れない。必要なら `Seat.is_llm` と `Player.role/alive` から `human_wolf_seats` を組み立てて pure function に渡す。
- max 2 狼の前提を維持する。もし human wolf が 2 人いて split した場合は、どちらかを勝手に優先しない。
- tests:
  - human wolf + LLM wolf が別 target を出したら human target が採用される。
  - human wolf 未提出 + LLM wolf 提出は pending/missing になる。
  - LLM wolf 2 人の split は従来どおり unresolved になる。
  - human wolf 2 人の split は従来どおり unresolved になる。

## 5. 噛み先不一致・夜行動未確定の公開漏洩を止め、DM で処理する

必要な仕様:
- main text channel、`/wolf status`、recovery announcement に、狼が 2 人残っていると推測できる情報を出さない。
- 公開表示では `WOLF_ATTACK`、`SEER_DIVINE`、`KNIGHT_GUARD` など role-identifying kind 名、対象 seat 名、未確定件数、`意見が割れました` のような split を示す文言を出さない。
- 投票 (`VOTE`, `RUNOFF_VOTE`) は公開情報なので、未提出者名を表示してよい。
- 夜の role-identifying pending は公開では generic にする。例: `秘密行動の未確定があります。該当者へ DM を送信しました。`
- WOLF_ATTACK split の詳細案内は main text や status ではなく、該当する人間 wolf への DM 再送と LLM wolf の再ディスパッチで処理する。
- wolves channel への bot 自動投稿でも、split 詳細・未確定 seat 名・件数を出さない。人狼同士が通常相談に使う wolves channel 自体は維持する。

実装方針:
- `discord_service.py` の `ROLE_IDENTIFYING_KINDS` 表示方針を強化する。
- `render_pending_host_lines()` は role-identifying kind を generic な 1 行にまとめ、kind 名・seat 名・件数を出さない。
- `announce_waiting()` は role-identifying pending を main text へ generic 表示し、WOLF_ATTACK 詳細を wolves channel へ自動投稿しない。
- `announce_recovery()` も role-identifying kind 名・件数を出さない。
- `GameService.resend_pending_dms()` は既存どおり missing + unresolved の union を DM 対象にしてよいが、公開通知とは分離する。
- 人間 wolf への DM 文面は「夜行動が未確定です。襲撃対象を再選択してください。」程度にし、相方が誰を選んだかは出さない。
- LLM wolf は `restrict_to_seats` と `unresolved_seats` を使って再ディスパッチし、既存の idempotency guard を壊さない。

tests:
- `tests/test_wolf_status_rendering.py` を更新し、role-identifying pending は generic 表示になり、`WOLF_ATTACK` / seat 名 / 件数 / `意見が割れました` が出ないことを確認する。
- `tests/test_discord_service.py` の waiting announcement テストを更新し、main text と wolves channel のどちらにも split 詳細が出ないことを確認する。
- vote pending は引き続き names が出ることを確認する。
- recovery announcement も role-identifying kind 名・件数を出さないことを確認する。

## 6. テストと fake を新仕様へ更新する

必要なテスト変更:
- `tests/fakes.py`
  - `FakeLLMAdapter` に新しい LLM discussion / runoff speech method を追加する。
  - 既存 method 名を残す場合は wrapper として記録し、新仕様の呼び出しを assert できるようにする。
- `tests/test_llm_trigger.py`
  - 旧 reactive trigger テストを削除または新仕様に置換する。
  - 昼 2 巡が seat 順で進むこと。
  - skip / API failure でも progress が進むこと。
  - phase/day stale check で古い発言が投稿されないこと。
  - 全 round 完了時に wake されること。
- `tests/test_game_service_advance.py`
  - LLM なしの `DAY_DISCUSSION` は deadline 到達で `DAY_VOTE` へ進むこと。
  - LLM ありで deadline 前に 2 巡完了しても `DAY_DISCUSSION` に留まること。
  - deadline 後でも 2 巡未完了なら `DAY_DISCUSSION` に留まること。
  - deadline 後かつ 2 巡完了なら `DAY_VOTE` へ進むこと。
  - same-phase wait transition で LLM round task を重複ディスパッチしないこと。
  - 通常投票 tie + LLM 候補ありで `DAY_RUNOFF_SPEECH` に入り、発言完了後に `DAY_RUNOFF` へ進むこと。
  - 通常投票 tie + LLM 候補なしで直接 `DAY_RUNOFF` へ進むこと。
- `tests/test_state_machine_votes.py`
  - 投票結果表示を投票者基準の期待値へ更新する。
  - tie 時の `DAY_RUNOFF_SPEECH` 分岐が pure transition として確認できるなら追加する。
- `tests/test_state_machine_nights.py` / `tests/test_rules_night_targets.py`
  - human wolf priority の pure rule test を追加する。
  - 従来 split behavior の non-regression を維持する。
- `tests/test_recovery.py`
  - `DAY_DISCUSSION` / `DAY_RUNOFF_SPEECH` で未完了 LLM speech がある場合に、recovery 後に再ディスパッチされること。
  - 完了済み progress は再実行されないこと。
- `tests/test_persistence_migrate.py` / `tests/test_repo_roundtrip.py`
  - 追加 column / progress helper の migration と roundtrip を確認する。

既存テストの注意:
- `tests/test_discord_on_message_log_gate.py` は、main text 発言の public log 保存 gate は維持しつつ、LLM reaction が呼ばれない新仕様に合わせる。
- `tests/test_llm_service.py` の prompt / vote / night action 系テストは壊さない。
- `tests/test_llm_structured_output.py`、`tests/test_llm_resolver.py`、`tests/test_llm_prompt_builder.py` は今回の仕様変更と無関係なら最小変更に留める。

## 7. 受け入れ条件

- `DAY_DISCUSSION` は LLM 2 巡完了と制限時間経過の両方を満たした後にのみ `DAY_VOTE` へ進む。
- main text の人間発言で LLM が追加反応しない。
- LLM なしの村は従来どおり時間で昼議論が終わる。
- 投票結果ログは投票者基準で表示される。
- 決選候補に LLM がいる場合、候補 LLM 発言が終わってから決選投票 DM / LLM 決選投票が始まる。
- 人間 wolf + LLM wolf の襲撃先不一致は人間 wolf の合法な提出先を採用する。
- LLM wolf 同士や人間 wolf 同士の split は従来どおり unresolved として再提出処理される。
- WOLF_ATTACK split や夜 role-identifying pending の kind / 件数 / seat 名 / split 文言が main text、status、recovery announcement に出ない。
- 夜の未確定処理は DM 再送と LLM 再ディスパッチで進む。
- bot 再起動後も `DAY_DISCUSSION` と `DAY_RUNOFF_SPEECH` の LLM progress を復旧できる。

実行する検証コマンド:
- `uv run pytest tests`
- `uv run ruff check src tests`
- `uv run mypy`

最後に簡潔に報告すること:
- 何を変えたか
- どのファイルを変えたか
- 実行した検証コマンドと結果
- 残課題があればその内容
```
