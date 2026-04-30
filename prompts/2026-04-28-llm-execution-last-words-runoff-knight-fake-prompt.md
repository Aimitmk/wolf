# `wolfbot` 2026-04-28 LLM 処刑前遺言・決選偽 CO (霊媒/騎士) 強化プロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player が投票で処刑されることが確定した局面で、即座に無言で退場せず、最後に盤面へ情報・推理・CO を残してから処刑されるようにすることです。加えて、決選投票の候補に人狼または狂人 LLM が入った場合、霊媒 CO が公開ログ上に出ていないなら投票回避のための霊媒騙りを最優先で検討し、霊媒 CO 既出かつ騎士 CO 未出なら騎士騙りを次点として検討できるようにします。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player に、処刑確定後の「遺言」発言を追加する。
- LLM player が処刑されることが投票で確定した場合、死亡処理の前に 1 回だけ公開発言してから退場させる。
- 処刑確定 LLM が占い師・霊媒師・騎士などの役職持ちで、公開ログ上まだ CO していない場合は、その遺言で CO し、持っている結果や護衛履歴を残すよう prompt と進行を更新する。
- 決選投票候補に人狼または狂人の LLM が含まれる場合、公開ログ上まだ霊媒 CO が出ていないなら霊媒騙りを最優先の投票回避策として、霊媒 CO は出ているが騎士 CO がまだなら騎士騙りを次点として、選択肢へ入れる。
- ただし、ゲームルール、9 人村固定配役、投票集計、勝敗条件、秘匿情報の境界は変えない。

今回必ず対応すること:
1. DAY_VOTE / DAY_RUNOFF で LLM seat の処刑が確定した場合、即 `_apply_execution()` せず、処刑前遺言用の中間 phase に入ること。
2. 処刑前遺言 phase では、処刑対象 LLM だけが 1 回公開発言すること。他の LLM や人間候補は発言待ちしない。
3. LLM の遺言発言試行が成功・skip・空文字・API 失敗のいずれでも、進行が止まらないよう完了扱いにすること。
4. 遺言完了後に、従来と同じ処刑ログ、死亡処理、権限更新、勝敗判定、夜遷移または GAME_OVER を行うこと。
5. 処刑確定 LLM が `SEER` / `MEDIUM` / `KNIGHT` で、公開ログ上まだ自分が CO していないと判断できる場合は、遺言で CO し、占い結果・霊媒結果・護衛履歴を可能な範囲で出すよう task prompt を追加すること。
6. 処刑確定 LLM が人狼または狂人で、勝ち筋上 CO 騙りが有効なら、遺言で偽占い・偽霊媒・偽騎士の主張を選択肢に入れてよい。ただし破綻しやすい作り話を強制しないこと。
7. 決選投票前の `DAY_RUNOFF_SPEECH` で、候補 LLM が `WEREWOLF` または `MADMAN` の場合、(a) 公開ログ上まだ霊媒 CO がないなら霊媒騙りを投票回避の最優先策として検討させ、(b) 霊媒 CO は既出だが騎士 CO がまだなら騎士騙りを次点として検討させること。霊媒騙りは護衛履歴を背負わないぶん破綻しにくいため優先する。
8. 既存の fire-and-forget LLM 実行、DB progress、wake、recovery の設計に揃えること。
9. slash command、Discord UI、投票 resolver、`LLMAction` schema は変更しないこと。

最重要ルール:
- まず既存実装を読み、現在の `DAY_RUNOFF_SPEECH`、`llm_speech_counts`、`GameService._dispatch_submissions()`、`RecoveryService`、LLM prompt 構築の流れを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つ。Discord I/O、LLM I/O、DB I/O を domain に入れないこと。
- `GameService.advance()` の通常パスで LLM API を長時間 await しないこと。遺言も既存の昼 2 巡 / 決選前発言と同じく fire-and-forget + persisted progress + wake で扱うこと。
- `SqliteRepo.apply_transition(..., expected_phase=...)` の optimistic lock を迂回しないこと。
- Discord channel history を直接 prompt に入れないこと。LLM 文脈は既存どおり DB の public/private logs から構築すること。
- CO 判定 parser や公開ログ集計 parser を新規実装しないこと。CO 済みかどうかは LLM が raw public logs を読んで判断する。
- 秘密情報を main text channel、公開 status、recovery announcement に漏らさないこと。
- 人狼・狂人の偽 CO 戦術を、村役職・村人 prompt に漏らさないこと。
- 実装後は必ず関連 pytest、ruff、mypy を走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/domain/enums.py`
- `src/wolfbot/domain/models.py`
- `src/wolfbot/domain/rules.py`
- `src/wolfbot/domain/state_machine.py`
- `src/wolfbot/services/game_service.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/services/recovery_service.py`
- `src/wolfbot/persistence/schema.py`
- `src/wolfbot/persistence/sqlite_repo.py`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `tests/fakes.py`
- `tests/test_state_machine_votes.py`
- `tests/test_game_service_advance.py`
- `tests/test_llm_service.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_recovery.py`
- `tests/test_persistence_migrate.py`
- `tests/test_repo_roundtrip.py`

このリポジトリで確認済みの事実:
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md`。
- system prompt は `src/wolfbot/llm/prompt_builder.py::build_system_prompt()` が合成する。
- `task_daytime_speech()`, `task_vote()`, `task_night_action()`, `task_wolf_chat()` は `src/wolfbot/llm/prompt_builder.py` にある。
- `task_daytime_speech()` は day 2 以降の 1 巡目で占い師・霊媒師・騎士の能力結果公開を促している。
- `task_vote()` は人狼 voter にだけ相方情報を使った投票 discipline を追加する optional 引数を持つ。
- `DAY_RUNOFF_SPEECH` は既に存在し、通常投票の同票候補に LLM が含まれる場合、その候補 LLM だけが発言してから `DAY_RUNOFF` に進む。
- `llm_speech_counts` には `discussion_rounds_done` と `runoff_speech_done` があり、昼 2 巡と決選前発言の progress を永続化している。
- `GameService._plan_next()` は `DAY_DISCUSSION` と `DAY_RUNOFF_SPEECH` で progress 完了を確認し、未完了なら同 phase grace transition で待つ。
- `LLMAdapter.submit_llm_discussion_rounds()` と `submit_llm_runoff_candidate_speeches()` は fire-and-forget で background task を作り、成功/skip/失敗に関係なく progress を進めて wake する。
- `plan_day_vote_resolve()` と `plan_day_runoff_resolve()` は、現在は処刑確定時に `_apply_execution()` を直接呼ぶ。
- `_apply_execution()` は処刑ログ作成、死亡 update、勝敗判定、夜遷移または GAME_OVER 遷移をまとめて行っている。
- 投票結果表示はすでに voter keyed 形式で、`_format_vote_results_by_voter()` を使っている。
- `RecoveryService` は復旧時に `GameService.resend_pending_dms()` と `resume_llm_speech_progress()` を呼び、`DAY_DISCUSSION` / `DAY_RUNOFF_SPEECH` の LLM speech を再ディスパッチできる。
- legacy の CO parser は削除済みで、`build_user_context()` は raw public log を渡し、LLM 自身が公開ログを読む方針になっている。

実装要求

## 1. 処刑前遺言 phase を追加する

必要な仕様:
- `DAY_VOTE` または `DAY_RUNOFF` の投票解決で、処刑対象が LLM seat だった場合、即死亡させず `DAY_EXECUTION_SPEECH` phase に入る。
- 処刑対象が人間 seat の場合は従来どおり即 `_apply_execution()` する。
- `DAY_EXECUTION_SPEECH` は処刑対象 LLM の 1 回だけの公開発言を待つ中間 phase とする。
- 処刑対象 LLM 以外は、この phase で新しく発言しない。
- 人間からの入力待ち phase ではないため、`WAITING_HOST_DECISION` や vote pending と混ぜない。
- 遺言発言の safety deadline は 60 秒を推奨する。定数名は `EXECUTION_SPEECH_DEADLINE = 60`、grace は `EXECUTION_SPEECH_GRACE = 30` を推奨する。
- LLM の発言試行が完了したら、成功・skip・空文字・API 失敗を問わず処刑解決へ進む。
- API 失敗や bot 再起動で phase が永遠に止まらないこと。

実装方針:
- `Phase.DAY_EXECUTION_SPEECH` を追加する。
- `state_machine.py` に以下相当の pure helpers を追加する。
  - `plan_execution_speech_wait(game, now_epoch) -> Transition`
  - `plan_execution_speech_to_execution(game, players, seats_by_no, executed_seat, now_epoch, *, clear_force, source_round) -> Transition`
- `plan_execution_speech_to_execution()` は内部で既存 `_apply_execution()` を呼ぶ。死亡処理・勝敗判定・夜遷移は既存 helper に集約し続ける。
- `plan_day_vote_resolve()` と `plan_day_runoff_resolve()` は、処刑対象が LLM seat の場合 `_apply_execution()` ではなく `DAY_EXECUTION_SPEECH` へ遷移する。
- `DAY_EXECUTION_SPEECH` へ入る public log は、処刑対象確定を伝えるが、まだ死亡扱いにしない。
  - 例: `投票の結果、{name} が処刑対象に決まりました。遺言を待っています。`
  - 投票結果 block はここに付けてよい。
- 遺言完了後の正式 `EXECUTION` log は従来の `{name} が処刑されました。` を維持する。
- 投票結果 block は二重投稿しない。推奨は `DAY_EXECUTION_SPEECH` への遷移ログにだけ付け、正式 `EXECUTION` log では `tally_suffix=""` にすること。
- 処刑対象の seat_no は DB に新規保存しなくてよい。`DAY_EXECUTION_SPEECH` では現 day の vote rows から deterministic に再計算する。
  - round 1 votes が存在する場合は、round 0 の tied candidates を再計算し、その候補集合で round 1 を解決する。
  - round 1 votes が存在しない場合は、round 0 を解決する。
  - どちらでも executed が得られない場合は defense-in-depth として `plan_execution_speech_wait()` で短く待つか、no-execution fallback を明示的に実装する。
- `GameService._plan_next()` に `DAY_EXECUTION_SPEECH` 分岐を追加し、処刑対象 LLM の `execution_speech_done` を確認してから正式処刑へ進む。
- `GameService._dispatch_submissions()` は `DAY_EXECUTION_SPEECH` 初回 entry でだけ LLM 遺言 task をディスパッチし、same-phase grace re-commit では重複ディスパッチしない。
- 権限更新は、`DAY_EXECUTION_SPEECH` へ入る時点では対象 LLM をまだ生存扱いにする。`_apply_execution()` 後の transition で初めて `newly_dead_seats` と `kill_permissions()` が動く。

注意:
- 処刑対象 LLM が遺言中にまだ公開チャンネルへ発言できるよう、死亡 update は遺言後にだけ行う。
- 遺言 phase 中の `/wolf status` は通常の phase 表示でよいが、未提出者として人間名を出す必要はない。
- `DAY_EXECUTION_SPEECH` で force-skip pending を使わない。進行停止を避けるため、LLM task 側の finally と grace deadline で進める。

## 2. 遺言 progress を永続化する

必要な仕様:
- 処刑対象 LLM の遺言が完了したかを DB に保存する。
- 完了保存は、発言成功・skip・空文字・decider 例外のすべてで行う。
- 再起動や重複 dispatch があっても、同じ LLM の遺言を二重投稿しない。

実装方針:
- 既存 `llm_speech_counts` に `execution_speech_done INTEGER NOT NULL DEFAULT 0` を追加する。
- `schema.py` の `CREATE TABLE IF NOT EXISTS llm_speech_counts` と `migrate()` の additive migration の両方を更新する。
- `SqliteRepo` に以下相当を追加する。
  - `mark_llm_execution_speech_done(game_id, day, seat_no)`
  - `load_llm_speech_progress()` の戻り値へ `execution_speech_done` を追加する、または専用 loader を追加する。
- 既存 tests が tuple index に依存しているため、戻り値拡張で多くの更新が必要なら専用 loader の方が安全。推奨は専用 helper:
  - `load_llm_execution_speech_done(game_id, day, seat_no) -> bool`
- `mark_llm_execution_speech_done()` は idempotent UPSERT にする。
- `tests/test_persistence_migrate.py` と `tests/test_repo_roundtrip.py` に migration / roundtrip test を追加する。

## 3. LLM 遺言 task を追加する

必要な仕様:
- 処刑対象 LLM は、80〜300 字程度で最後の公開発言をする。
- 発言は `intent=speak` と `public_message` を基本にする。話すことがない場合のみ `intent=skip` を許容する。
- 遺言では、以下を短く優先する。
  - 自分視点の最重要推理
  - 今日の投票・CO・判定・噛み筋から残すべき疑い先
  - 自分が真役職なら、未公開の CO と結果・履歴
  - 自分が狼陣営なら、村を誤誘導する最後の主張。ただし露骨な自白や内部情報漏洩は避ける。
- 遺言は長文の内部思考ではなく、公開ログとして自然な最後の発言にする。

実装方針:
- `src/wolfbot/llm/prompt_builder.py` に `task_last_words(day_number: int, *, role: Role | None = None) -> str` を追加する。
- `__all__` に `task_last_words` を追加する。
- `LLMAdapter.submit_llm_execution_speech(...)` と `_run_execution_speech(...)`、`_do_one_execution_speech(...)` 相当を追加する。
- `_do_one_execution_speech()` は `_do_one_discussion_speech()` / `_do_one_runoff_speech()` と同じ投稿・ログ保存パターンにする。
  - `message_poster.post_public(fresh, f"**{seat.display_name}**: {message}", kind="LLM_SPEAK")`
  - `logs_public` には `kind="PLAYER_SPEECH"`, `actor_seat=player.seat_no` で保存する。
- stale check は `fresh.phase is Phase.DAY_EXECUTION_SPEECH` と `fresh.day_number == game.day_number` を必ず見る。
- progress は `finally` で `mark_llm_execution_speech_done()` する。
- 全処理後に `gs.wake.wake(game.id)` する。
- `GameService.LLMAdapter` Protocol、`tests/fakes.py::FakeLLMAdapter` に新 method を追加する。

`task_last_words()` に必ず含める趣旨:
- 「あなたは投票で処刑されることが確定しています。死亡処理の前に最後の公開発言を 1 回だけできます。」
- 「村陣営なら、残る生存者が次に使える情報を最優先で残してください。」
- 「人狼陣営なら、露骨な自白や相方漏洩を避け、最後まで勝ち筋が残る主張をしてください。」
- `role is Role.SEER`:
  - 公開ログ上まだ占い師 CO していないなら、占い師 CO し、初日ランダム白と以後の占い結果を時系列で出す。
  - すでに CO 済みなら、判定履歴と今日の処刑後に見るべき狼候補を短く残す。
- `role is Role.MEDIUM`:
  - 公開ログ上まだ霊媒師 CO していないなら、霊媒師 CO し、これまでの処刑者への霊媒結果を時系列で出す。
  - 処刑がなかった日は結果なしとして扱い、存在しない結果を作らない。
- `role is Role.KNIGHT`:
  - 公開ログ上まだ騎士 CO していないなら、騎士 CO し、合法な護衛履歴を日付順に出す。
  - 自分護衛・同一対象連続護衛・死亡済み対象護衛・死者が出た朝の不自然な護衛成功主張はしない。
  - 平和な朝があったなら、護衛成功可能性と護衛先を短く示す。
- `role is Role.WEREWOLF`:
  - 相方や夜の真の襲撃先など、村人が知り得ない内部情報を漏らさない。
  - 偽 CO をするなら、決選前発言と同じ優先順で選ぶ。
    - (1) 公開ログ上まだ霊媒 CO が出ていないなら、霊媒騙りを最優先する (護衛履歴を持たないため破綻リスクが低い)。
    - (2) 霊媒 CO 既出かつ騎士 CO 未出なら、騎士騙りを次点で検討する (合法な護衛履歴を日付順に出し、自分護衛・連続護衛・死亡済み対象護衛・死者が出た朝の護衛成功主張は避ける)。
    - (3) 両方既出なら、占い騙りや疑い返しを比較する。
  - 偽 CO が破綻しやすい場合は強行せず、最後に勝ち筋が残る推理を残す。
- `role is Role.MADMAN`:
  - 本物の人狼位置を知っている前提で話さない。
  - 偽 CO を残すなら、人狼と同じ優先順 (霊媒騙り → 騎士騙り → 占い騙り) を使う。誤爆リスクと公開ログ整合を意識する。
- `role is Role.VILLAGER`:
  - 村人 CO で信用を取ろうとせず、公開情報からの推理を残す。

注意:
- `task_last_words()` は実際の CO 済み判定をコードで行わない。公開ログ上 CO 済みかどうかは LLM 自身が判断する。
- ただし prompt には「公開ログ上まだ CO していないなら」と明記する。
- 能力結果や護衛履歴に必要な私的情報は、既存 private logs から読ませる。新しい role leak を user context に追加しない。
- 現在の private logs だけでは騎士の護衛履歴が不足する場合、騎士本人にだけ `previous_guard` / night action history を安全に渡す最小補助を検討してよい。ただし全役職共通 context には入れない。

## 4. 決選前発言で狼・狂人の偽 CO 戦術を強化する (霊媒騙り → 騎士騙りの優先順)

必要な仕様:
- 通常投票同票で `DAY_RUNOFF_SPEECH` に入ったとき、同票候補 LLM が `WEREWOLF` または `MADMAN` なら、通常の `task_daytime_speech()` ではなく決選回避用の task を使う。
- 偽 CO の優先順は以下の通りとする。
  - (1) 公開ログ上まだ霊媒 CO が出ていないなら、霊媒騙りを最優先で検討する。霊媒騙りは護衛履歴を持たないため破綻リスクが低い。
  - (2) 霊媒 CO は既に出ているが騎士 CO がまだ出ていないなら、騎士騙りを次点として検討する。
  - (3) 霊媒 CO・騎士 CO の両方が既に出ているなら、占い騙り・投票理由の反論・対抗候補への疑い返しを比較する。
- いずれの偽 CO も強制ではない。発言済みの整合や CO 数バランス、過去発言との矛盾、相方位置との衝突から破綻しやすい場合は、別の弁明や疑い返しに切り替えてよい。
- 人狼と狂人で秘匿情報の扱いを分ける。
  - 人狼は相方情報を漏らさず、自分と相方の勝ち筋を考える。
  - 狂人は本物の人狼位置を知らないため、狼を知っている前提の発言をしない。
- 非狼陣営の決選前発言には、この狼陣営専用の偽 CO 優先順 guidance を入れない。

実装方針:
- `prompt_builder.py` に `task_runoff_candidate_speech(day_number: int, *, role: Role | None = None) -> str` を追加する。
- 既存 `_do_one_runoff_speech()` は `task_daytime_speech(game.day_number, role=player.role)` ではなく、`task_runoff_candidate_speech(game.day_number, role=player.role)` を使う。
- `task_runoff_candidate_speech()` の base は「あなたは決選投票候補です。投票前に 1 回だけ公開発言できます。自分への投票を避けるため、疑いへの反論、投票すべき対抗候補、CO/結果/履歴がある場合の提示を短く行う」とする。
- `role in (Role.WEREWOLF, Role.MADMAN)` のときだけ、以下の wolf-side block を追加する。

狼陣営向け決選前発言 block に必ず含める趣旨:
- 「公開ログ上まだ霊媒 CO が出ていないなら、処刑回避のために霊媒騙りを最優先で検討する。霊媒騙りは護衛履歴を持たないため、騎士騙りより破綻リスクが低い。」
- 「霊媒騙りをする場合は、day 1 ならまだ処刑が発生していないので結果なしとして CO する。day 2 以降は前日処刑者への霊媒結果 (◯/●) を整合的に出す。day 3 以降は霊媒結果を時系列で並べる。」
- 「霊媒 CO は既に出ているが騎士 CO がまだ出ていないなら、騎士騙りを次点として検討する。騎士騙りをする場合は、合法な護衛履歴を日付順に出し、自分護衛・同一対象連続護衛・死亡済み対象護衛・死者が出た朝の護衛成功主張は破綻になるため避ける。平和な朝があった場合だけ、護衛成功主張と護衛先が自然かを確認する。」
- 「霊媒 CO・騎士 CO の両方が既に出ている場合、新たな偽 CO は対抗 CO 扱いとなり破綻リスクが高い。占い騙り・投票理由の反論・対抗候補への疑い返しを比較する。」
- 「いずれの偽 CO も、過去発言と整合せず矛盾しそうなら強行しない。投票回避が達成できる代替策と比較する。」

文面例:

```text
あなたは決選投票候補です。投票前に 1 回だけ公開発言できます。自分が処刑されないため、疑いへの反論、対抗候補へ投票すべき理由、必要なら CO を短く出してください。

あなたは狼陣営です。投票回避のための偽 CO は、以下の優先順で検討してください。
1. 公開ログ上まだ霊媒 CO が出ていないなら、霊媒騙りを最優先する。霊媒騙りは護衛履歴を背負わないため破綻リスクが低い。day 1 なら結果なしとして CO し、day 2 以降は前日処刑者への霊媒結果を整合的に並べる。
2. 霊媒 CO は既に出ているが騎士 CO がまだなら、騎士騙りを次点で検討する。合法な護衛履歴を日付順に出し、自分護衛・同一対象連続護衛・死亡済み対象護衛・死者が出た朝の護衛成功主張は避ける。
3. 霊媒 CO・騎士 CO 両方が既に出ているなら、占い騙り・反論・対抗候補への疑い返しを比較する。
過去発言と整合しない、CO 数増加で非 CO 位置が確白化する、相方位置と衝突するなどで破綻リスクが高ければ、偽 CO を強行せず別の弁明に切り替えてください。
```

注意:
- `task_runoff_candidate_speech()` は `LLMAction` schema を変えず、`intent=speak` / `public_message` を使う。
- 「霊媒 CO / 騎士 CO が出ているか」の判定は parser で行わず、LLM が public logs を読む。
- 偽 CO 優先順の文面は `Role.WEREWOLF` と `Role.MADMAN` の task にだけ出す。`Role.SEER` / `Role.MEDIUM` / `Role.KNIGHT` / `Role.VILLAGER` には出さない。

## 5. 正式処刑への進行を実装する

必要な仕様:
- `DAY_EXECUTION_SPEECH` の遺言完了後、投票で確定した同じ seat を処刑する。
- 遺言中に対象 seat がまだ alive であることを前提にしつつ、corrupt / stale state には defense-in-depth を入れる。
- 勝利判定は従来どおり処刑直後に行う。
- 処刑後の夜開始、DM / LLM 夜行動 dispatch、`newly_dead_seats` による権限更新は従来と同じ順序で動く。

実装方針:
- `GameService._plan_next()` の `DAY_EXECUTION_SPEECH` 分岐で以下を行う。
  1. current day の votes を読み、処刑予定 seat を再計算する。
  2. その seat が LLM であることを確認する。
  3. `execution_speech_done` を確認する。
  4. 未完了かつ deadline 未到達なら `None` を返して待つ。
  5. deadline 到達かつ未完了なら `plan_execution_speech_wait()` で short grace にする。
  6. 完了済みなら `plan_execution_speech_to_execution()` で `_apply_execution()` へ進める。
- `source_round` は正式処刑 log には出さなくてよい。test や helper の明確化用に保持するだけでよい。
- `DAY_EXECUTION_SPEECH` への transition で `clear_force_skip=True` にするかどうかは既存 vote 解決の挙動に合わせる。推奨は、投票解決が済んだ時点で vote の force-skip を clear する。
- 正式処刑 transition では `clear_force_skip` を再度 true にしてもよいが、不要な重複更新は避ける。

## 6. Recovery と resend を更新する

必要な仕様:
- bot 再起動時に `DAY_EXECUTION_SPEECH` で止まっていた場合、遺言未完了なら再ディスパッチする。
- 遺言完了済みなら再ディスパッチしない。engine wake / deadline により正式処刑へ進む。
- `resend_pending_dms()` は人間 DM 用なので、`DAY_EXECUTION_SPEECH` では原則 no-op のままでよい。

実装方針:
- 既存 `GameService.resume_llm_speech_progress()` を拡張し、`DAY_EXECUTION_SPEECH` で未完了の処刑対象 LLM がいる場合に `submit_llm_execution_speech()` を呼ぶ。
- 処刑対象 seat の再計算 helper は `_plan_next()` と recovery resume の両方で使えるよう、`GameService` 内の private helper に切り出す。
- `RecoveryService` は既に `resume_llm_speech_progress()` を呼んでいるので、呼び出し箇所を増やさず helper 側対応で済ませる。

## 7. 情報秘匿と role leak を壊さない

必要な仕様:
- 遺言発言は公開ログに出るため、LLM 自身が公開してよい内容だけを話す。
- 真役職 LLM は自分の私的結果を公開してよいが、他者の実役職一覧や他者の private logs を見てはいけない。
- 人狼 LLM は相方情報を知っているが、公開遺言や決選前発言で「相方」として漏らしてはいけない。
- 狂人 LLM は本物の人狼位置を知らない。狂人 prompt に本物の狼位置や相方情報を渡さない。
- 非狼陣営 prompt に、狼陣営専用の「処刑回避のための霊媒騙り / 騎士騙りの優先順」指示が混ざってはいけない。

やってはいけないこと:
- 投票結果を code 側で変更する。
- LLM の投票先を code 側で強制変更する。
- CO parser / role claim parser を再導入する。
- Discord API から channel history を直接読んで prompt に渡す。
- `LLMAction` schema を変更する。
- DB schema の destructive migration を行う。
- slash command や DM UI を追加・変更する。
- 人間 player に遺言入力 UI を追加する。
- 無関係な refactor。

## 8. テストを追加 / 更新する

`tests/test_state_machine_votes.py`:
- 通常投票で LLM seat が unique plurality になった場合、`DAY_EXECUTION_SPEECH` へ遷移し、まだ `player_updates` が空であること。
- 通常投票で人間 seat が unique plurality になった場合、従来どおり即 `NIGHT` または `GAME_OVER` に進むこと。
- 決選投票で LLM seat が処刑確定した場合、`DAY_EXECUTION_SPEECH` へ遷移すること。
- `DAY_EXECUTION_SPEECH` 開始 log に投票結果 block が入り、正式 `EXECUTION` log で二重表示しない設計なら、その期待値を固定すること。

`tests/test_game_service_advance.py`:
- DAY_VOTE 解決で LLM 処刑確定時、`DAY_EXECUTION_SPEECH` に入り、`submit_llm_execution_speech` が dispatch されること。
- `execution_speech_done=False` かつ deadline 前なら進まないこと。
- deadline 後で未完了なら same-phase grace transition になり、重複 dispatch しないこと。
- `execution_speech_done=True` なら正式処刑へ進み、死亡 update、`kill_permissions()`、夜 action dispatch が従来どおり起きること。
- 処刑で勝利条件を満たす場合、遺言後に `GAME_OVER` と role reveal が出ること。

`tests/test_llm_service.py`:
- `submit_llm_execution_speech()` は fire-and-forget で返り、decider 完了を待たないこと。
- `_do_one_execution_speech()` が `LLM_SPEAK` 投稿と `PLAYER_SPEECH` public log 保存を行うこと。
- phase/day stale check により、古い遺言が投稿されないこと。
- decider が skip / exception / empty message でも `mark_llm_execution_speech_done()` が呼ばれること。
- 遺言完了後に wake されること。
- `DAY_RUNOFF_SPEECH` の wolf/madman 候補では `task_runoff_candidate_speech()` の偽 CO 優先順 guidance (霊媒騙り → 騎士騙り) が system prompt に入ること。
- villager/seer/medium/knight 候補の決選前発言には狼陣営専用の偽 CO 優先順 guidance が入らないこと。

`tests/test_llm_prompt_builder.py`:
- `task_last_words(day, role=Role.SEER)` に `占い師 CO`、`初日ランダム白`、`占い結果`、`時系列` が含まれること。
- `task_last_words(day, role=Role.MEDIUM)` に `霊媒師 CO`、`前日処刑者`、`霊媒結果`、`結果なし` が含まれること。
- `task_last_words(day, role=Role.KNIGHT)` に `騎士 CO`、`護衛履歴`、`自分護衛`、`同一対象連続護衛`、`死亡済み対象` が含まれること。
- `task_last_words(day, role=Role.WEREWOLF)` に内部情報漏洩禁止、相方漏洩禁止、霊媒騙り優先・騎士騙り次点の優先順、勝ち筋を残す主張が含まれること。
- `task_last_words(day, role=Role.MADMAN)` に本物の人狼位置を知らない前提、偽 CO 優先順 (霊媒 → 騎士 → 占い) が含まれること。
- `task_runoff_candidate_speech(day, role=Role.WEREWOLF)` と `Role.MADMAN` に、霊媒 CO 未出なら霊媒騙り優先・霊媒 CO 既出かつ騎士 CO 未出なら騎士騙り次点という優先順文面が含まれること。
- `task_runoff_candidate_speech()` の非狼 roles (villager/seer/medium/knight) に、狼陣営専用の偽 CO 優先順文面が漏れないこと。

`tests/test_recovery.py`:
- `DAY_EXECUTION_SPEECH` で未完了の LLM 遺言がある場合、recovery 後に `submit_llm_execution_speech()` が呼ばれること。
- 完了済みなら呼ばれないこと。

`tests/test_persistence_migrate.py` / `tests/test_repo_roundtrip.py`:
- old `llm_speech_counts` table に `execution_speech_done` が追加されること。
- `mark_llm_execution_speech_done()` が idempotent に保存できること。
- missing row の default が false になること。

`tests/fakes.py`:
- `FakeLLMAdapter` に `submit_llm_execution_speech()` を追加し、呼び出し seat を assert できるよう記録すること。

既存テスト群は壊さないこと:
- `tests/test_state_machine_votes.py`
- `tests/test_game_service_advance.py`
- `tests/test_llm_service.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_recovery.py`
- `tests/test_persistence_migrate.py`
- `tests/test_repo_roundtrip.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`

## 9. 受け入れ条件

- LLM player が通常投票または決選投票で処刑されることが確定した場合、死亡処理の前に 1 回だけ遺言発言の機会を得る。
- 遺言発言が成功した場合、main text に LLM 発言として投稿され、DB public log に `PLAYER_SPEECH` として残る。
- 遺言発言が失敗・skip・空文字でも、ゲームは停止せず正式処刑へ進む。
- 処刑対象が人間の場合は挙動を変えない。
- 真占い師・真霊媒師・真騎士の LLM は、未 CO のまま処刑されそうな場合、遺言で CO と結果/履歴を残す方針を prompt で受け取る。
- 人狼・狂人 LLM は、決選候補に入ったとき、霊媒 CO 未出なら霊媒騙りを最優先、霊媒 CO 既出かつ騎士 CO 未出なら騎士騙りを次点として、投票回避策に積極的に検討できる。
- 非狼 role prompt に狼陣営専用の偽 CO 優先順 guidance が漏れない。
- 既存の `DAY_RUNOFF_SPEECH`、昼 2 巡、投票結果表示、人間狼襲撃優先、夜未確定秘匿の挙動を壊さない。
- `uv run pytest ...`、`uv run ruff check src tests`、`uv run mypy` が通る。

実行する検証コマンド:
- `uv run pytest tests/test_state_machine_votes.py tests/test_game_service_advance.py tests/test_llm_service.py tests/test_llm_prompt_builder.py tests/test_recovery.py tests/test_persistence_migrate.py tests/test_repo_roundtrip.py tests/test_llm_structured_output.py tests/test_llm_resolver.py`
- `uv run ruff check src tests`
- `uv run mypy`

最後に簡潔に報告すること:
- 何を変えたか
- どのファイルを触ったか
- 実行したテスト / lint / 型チェックの結果
- 実行できなかった検証があれば、その理由
```
