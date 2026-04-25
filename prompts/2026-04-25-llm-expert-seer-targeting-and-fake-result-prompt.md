# `wolfbot` 2026-04-25 LLM Expert Seer Targeting and Fake Result Prompt

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player を強い熟練した人狼プレイヤーに近づけることです。特に、真占い師 LLM の占い先選定を強化し、人狼・狂人 LLM が day 2 以降に役職騙りをする場合、該当役職として前夜に能力を使った想定の結果を 1 巡目の発言で自然に添えられるようにします。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player を、より強い熟練した 9 人村プレイヤーに近づける。
- 真占い師 LLM が夜の占い対象を選ぶとき、合法候補からランダム寄りに選ぶのではなく、翌日の議論・処刑・CO 真偽比較に最も情報を落とす対象を選べるよう prompt / task を強化する。
- 人狼・狂人 LLM が占い師・霊媒師・騎士を騙る場合、day 2 以降の昼 1 巡目の発言で、その役職として前夜に能力を使った想定の結果を必ず添えるよう system prompt / role strategy / daytime task を強化する。
- 偽の能力対象・偽結果は、この bot の実ルール、公開ログ、過去の自分の主張、生存状況、処刑・襲撃履歴と矛盾しない範囲で、熟練者らしく戦術的に選ばせる。
- 変更は LLM prompt / task text / LLM service の発言ラウンド文脈 / prompt test 周辺に閉じる。ゲームルール、DB schema、Discord command、状態遷移、投票・夜行動の解決ロジック、権限管理、復旧処理は変えない。

今回必ず対応すること:
1. 真占い師 strategy と `SEER_DIVINE` 夜行動 task に、熟練者向けの占い先選定軸を追加すること。
2. `DAY_DISCUSSION` の LLM 昼発言 task に「何巡目の発言か」を渡せるようにし、day 2 以降の 1 巡目だけ、CO 中または CO 予定の情報役は前夜結果をこの発言で添えるよう明記すること。
3. 人狼 strategy に、占い師・霊媒師・騎士騙りで day 2 以降の 1 巡目に前夜結果を出すこと、偽対象・偽結果を相方位置、襲撃計画、囲いリスク、票筋、霊媒結果と整合させることを追加すること。
4. 狂人 strategy に、占い師・霊媒師・騎士騙りで day 2 以降の 1 巡目に前夜結果を出すこと、ただし本物の人狼位置を知らないため黒誤爆・白先誤認リスクを踏まえて偽結果を選ぶことを追加すること。
5. 既存の `NIGHT_0` ランダム白、day 1 初回白、day 1 初回黒禁止、day 2 以降の黒出し検討、role-specific strategy 分離、structured output 制約、`target_name` 候補トークン厳密一致を壊さないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築、LLM 昼発言フロー、夜行動 task、role strategy、テストを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つ。今回の変更でルールエンジンや状態遷移を変えない。
- DB schema は変更しない。
- slash command は追加しない。
- Discord channel history を直接読んで prompt に入れてはいけない。LLM 文脈は既存どおり DB の public/private logs からだけ構築する。
- 非人狼 prompt に人狼相方情報や人狼専用連携語彙を漏らしてはいけない。
- 狂人 prompt に `相方`、`襲撃先を揃える`、本物の人狼位置を知っている前提の文面を入れてはいけない。
- user context に実役職推定や他者の実役職を追加してはいけない。
- LLM の `target_name` 解決、夜行動の合法候補生成、投票候補生成は既存の制約を維持する。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/llm/personas.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/domain/enums.py`
- `src/wolfbot/domain/rules.py`
- `src/wolfbot/domain/state_machine.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`
- `tests/test_rules_night_targets.py`

このリポジトリで確認済みの事実:
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md`。
- system prompt は `src/wolfbot/llm/prompt_builder.py::build_system_prompt()` が合成する。
- 共通ルールは `_build_game_rules_block()`、役職別戦略は `_ROLE_STRATEGIES`、昼発言 task は `task_daytime_speech()`、夜行動 task は `task_night_action()` にある。
- `LLMAdapter._run_discussion_rounds()` は day discussion で LLM を round 1 と round 2 の 2 巡発言させるが、現状 `round_idx` は `_do_one_discussion_speech()` / `task_daytime_speech()` に渡っていない。
- `LLMAdapter._do_one_runoff_speech()` も `task_daytime_speech()` を使っているため、signature を変える場合はデフォルト値で既存呼び出しを壊さないこと。
- `task_night_action(SubmissionType.SEER_DIVINE, ...)` は現在、合法候補から 1 名を選ぶ基本指示のみで、占い先の戦術的比較軸は十分に明示されていない。
- `task_night_action(SubmissionType.WOLF_ATTACK, ...)` と `task_night_action(SubmissionType.KNIGHT_GUARD, ...)` には、それぞれ役割別の判断軸がすでに追加されている。
- `NIGHT_0` ランダム白は本物の人狼ではない相手から選ばれ、day 1 に公開できる初回占い結果として扱われる。
- `domain/rules.py::is_detected_as_wolf(role)` は `Role.WEREWOLF` だけを黒扱いする。狂人は占い・霊媒で白に出る。
- 占い対象は生存中の自分以外。夜の襲撃で朝に死亡する相手を占っても占い結果は得られる。
- 霊媒師は当日処刑されたプレイヤーが本物の人狼かどうかだけを知る。処刑なしの日は霊媒結果なし。
- 騎士は自分護衛不可、同一対象の連続護衛不可、死亡済み対象の護衛不可。
- 既存の人狼・狂人 strategy には、day 1 占い師騙り、day 1 初回白、day 1 初回黒禁止、day 2 以降の黒出し検討、day 2 以降の霊媒師/騎士騙り時に結果を添える趣旨が一部ある。ただし、day 2 以降の昼 1 巡目で必ず前夜結果を出す指示と、偽対象・偽結果の熟練者向け選定軸はまだ弱い。

実装方針:
- 基本は prompt / task text の強化で対応する。
- `src/wolfbot/llm/prompt_builder.py` では、真占い師 strategy、狼 strategy、狂人 strategy、`task_daytime_speech()`、`task_night_action()` を更新する。
- `src/wolfbot/services/llm_service.py` では、discussion `round_idx` を `_do_one_discussion_speech()` に渡し、`task_daytime_speech(day_number, discussion_round=round_idx)` のように task へ渡す。
- `task_daytime_speech()` の公開関数名は維持する。signature は `task_daytime_speech(day_number: int, discussion_round: int | None = None) -> str` のように後方互換にする。
- `_do_one_runoff_speech()` は `task_daytime_speech(game.day_number)` のままでも動くようにする。runoff speech は今回の「day discussion 1 巡目」必須結果公開の対象にしない。
- 新しい DB カラム、永続化モデル、Discord UI、slash command、domain rule は追加しない。

## 1. 真占い師の占い先選定を強化する

`_ROLE_STRATEGIES[Role.SEER]` と `task_night_action(SubmissionType.SEER_DIVINE, ...)` に、以下の趣旨を追加すること。

必要な仕様:
- 占い対象は、単に怪しい相手ではなく、白でも黒でも翌日の議論に情報が落ちる位置を優先する。
- 高情報量の対象とは、以下のような位置である:
  - CO 真偽比較に効く位置
  - 対抗占い師の白先や囲い候補
  - 投票・決選投票・票変えで浮いた位置
  - 強い誘導をしているが根拠が薄い位置
  - 白なら進行役候補にでき、黒なら処刑提案しやすい位置
  - 自分視点の灰を狭め、翌日の吊り候補を明確にできる位置
- 優先度を下げる対象:
  - 既に自分が占った位置
  - 今日ほぼ処刑されそうな位置
  - 発言が極端に少なく、白黒どちらでも議論が伸びにくい位置
  - 占う前から公開情報で強く処理対象になっている位置
  - 今夜襲撃される可能性が非常に高く、結果を公開しても議論軸にしにくい位置。ただし、死亡しても結果は得られるため、情報価値が高ければ占ってよい。
- 対抗 CO がいる場合は、対抗の白先・黒先・投票先・対抗を庇う発言者を比較し、真偽判断に一番効く対象を選ぶ。
- 単独 CO 寄りで自分が信用されている場合は、黒狙いだけでなく、白が出たときに村の進行を安定させる位置も検討する。
- 黒を引いた翌朝は、結果・過去の白結果・その対象を占った理由を短く出し、処刑と霊媒確認を提案する。

`task_night_action(SubmissionType.SEER_DIVINE, ...)` に必ず含める文言の要素:
- `占い価値`
- `灰を狭める`
- `対抗 CO`
- `囲い候補`
- `投票`
- `白でも黒でも情報が落ちる`

推奨文面例:
- `占い対象は、白でも黒でも翌日の議論に情報が落ちる位置を優先してください。灰を狭める価値、対抗 CO の白先や囲い候補、投票・票変え、発言誘導との整合を比較して 1 名を選んでください。`
- `今日ほぼ処刑されそうな位置や、既に十分処理対象になっている位置は占い価値が下がります。ただし、結果が CO 真偽や囲い疑惑の整理に直結するなら候補に残してよいです。`

## 2. day 2 以降の昼 1 巡目 task に前夜結果公開ルールを追加する

`task_daytime_speech()` を後方互換のまま拡張すること。

必要な仕様:
- `DAY_DISCUSSION` の round 1 だけを `discussion_round=1` として task text に渡す。
- day 2 以降かつ `discussion_round == 1` のときだけ、次の趣旨を昼発言 task に追加する。
  - 自分が占い師・霊媒師・騎士として CO 済み、またはこの発言で CO するなら、前夜に能力を使った結果をこの 1 巡目発言で添える。
  - 占い師なら `対象 + 白/黒 + 占い理由` を短く出す。
  - 霊媒師なら `前日処刑者 + 人狼/人狼ではない/結果なし` を出す。
  - 騎士なら、CO する局面に限り `護衛日 + 護衛先 + 平和なら護衛成功主張` を合法な履歴として出す。
  - 結果を持つ、または結果を主張する役職 CO が結果を後回しにすると、熟練者目線では信用を落とす。
- day 1 にはこの追加指示を出さない。day 1 占い師 CO の初回結果は既存どおり `NIGHT_0` ランダム白ルールで扱う。
- round 2 や runoff speech では「必ず添える」までは言わない。ただし既に出した結果の補足や反論はしてよい。
- この task は全役職が見る可能性があるため、`相方` や人狼専用連携語彙は入れない。
- `public_message` の 80〜300 字目安は維持する。

実装の固定方針:
- `task_daytime_speech(day_number: int, discussion_round: int | None = None) -> str` に変更する。
- `LLMAdapter._run_discussion_rounds()` から `_do_one_discussion_speech(..., discussion_round=round_idx)` を渡す。
- `_do_one_discussion_speech()` は `task_daytime_speech(game.day_number, discussion_round=discussion_round)` を使う。
- `_do_one_runoff_speech()` は `task_daytime_speech(game.day_number)` を使い続ける。

推奨文面例:
- `day 2 以降の 1 巡目発言です。占い師・霊媒師・騎士として CO 済み、または今 CO する場合は、この発言で前夜相当の能力結果を添えてください。占い師なら対象と白黒、霊媒師なら前日処刑者への結果、騎士なら CO する局面で合法な護衛履歴を日付順に出します。`
- `結果を持つ、または結果を主張する CO が 1 巡目で結果を出さないと、信用低下や破綻疑いにつながります。`

## 3. 人狼の騙り結果・対象選びを熟練者向けに強化する

`_ROLE_STRATEGIES[Role.WEREWOLF]` に、以下の趣旨を追加すること。

必要な仕様:
- day 2 以降、占い師・霊媒師・騎士を騙る、または既に騙っている場合、昼 1 巡目では前夜相当の結果を必ず発表する。
- 偽占い師:
  - day 2 以降の結果は、前夜に占った想定として `対象 + 白/黒 + 短い理由` を出す。
  - 対象は、その夜開始時点で占える生存者に限定する。死亡済み対象、存在しない対象、自分自身、過去に矛盾する判定を出した対象を使わない。
  - 相方を白で囲う場合は、発言・投票・噛み筋と整合させる。露骨な囲いはラインを疑われる。
  - 非相方へ白を出す場合は、白位置噛みや SG 残し、対抗の信用落としと矛盾しないかを見る。
  - 黒を出す場合は、真役職・騎士・強い白位置への直撃で対抗 CO や破綻を招かないか、翌日の霊媒結果・投票・襲撃計画と整合するかを確認する。
- 偽霊媒師:
  - 前日処刑者への結果だけを出す。処刑なしの日は霊媒結果なしを主張し、存在しない結果を作らない。
  - 霊媒黒は、対象が人狼だった主張になるため、占いローラー停止や灰吊り誘導に使えるが、過去の投票・相方位置・残り縄と矛盾しないようにする。
  - 霊媒白は、対象が本物の人狼ではない主張になるため、真占い師だった可能性、狂人だった可能性、村役だった可能性をどう見せるかを整理する。
- 偽騎士:
  - CO するなら合法な護衛履歴を日付順に出す。
  - 自分護衛、同一対象連続護衛、死亡済み対象護衛、死亡者が出た朝の護衛成功主張はしない。
  - 平和な朝に護衛成功を主張する場合は、護衛先がその朝の襲撃失敗説明として自然か、襲撃計画と矛盾しないかを確認する。
- 結果を出す発言では、長い内部思考ではなく、結果と最も効く 1〜2 点の理由に絞る。

人狼 strategy に必ず含める文言の要素:
- `day 2 以降`
- `1 巡目`
- `前夜`
- `能力結果`
- `相方`
- `囲い`
- `噛み筋`
- `霊媒結果`
- `合法な護衛履歴`

注意:
- 人狼 strategy には `相方` や襲撃計画を書いてよい。
- ただし、非狼 role strategy にこの文面を漏らしてはいけない。

## 4. 狂人の騙り結果・対象選びを熟練者向けに強化する

`_ROLE_STRATEGIES[Role.MADMAN]` に、以下の趣旨を追加すること。

必要な仕様:
- day 2 以降、占い師・霊媒師・騎士を騙る、または既に騙っている場合、昼 1 巡目では前夜相当の結果を必ず発表する。
- 狂人は本物の人狼位置を知らないため、偽結果は「狼を助けるつもり」でも誤爆や誤支援が起きる前提で選ぶ。
- 偽占い師:
  - 白出しは破綻しにくいが、白先が本物の狼とは限らない。白先を確定の味方として扱わない。
  - 黒出しは day 2 以降でも誤爆リスクがある。本物の人狼、真役職、強く白い位置に当てた場合の反動を考える。
  - 黒先は、公開ログ上で処刑可能性があり、かつ自分の過去結果・対抗結果・投票履歴と矛盾しにくい位置を選ぶ。
  - 対抗占い師の白先へ黒を重ねる、灰に黒を出す、白を重ねて議論を割るなどの選択肢を、CO 数・縄数・霊媒状況で使い分ける。
- 偽霊媒師:
  - 前日処刑者への結果だけを出す。処刑なしの日は結果なし。
  - 霊媒結果は、真占い師の信用を落とす、霊媒ローラーに持ち込む、占いローラー停止/継続を歪める目的で使う。
  - ただし本物の人狼が処刑されたかは知らないため、黒白どちらも公開情報との整合性と誤支援リスクを見る。
- 偽騎士:
  - 終盤や対抗騎士が出た場面で検討する。
  - 合法な護衛履歴だけを出す。自分護衛、連続護衛、死亡済み護衛、存在しない護衛成功は主張しない。
- 狂人 strategy には、人狼専用の `相方`、`襲撃先を揃える`、本物の狼位置を知っている前提を絶対に入れない。

狂人 strategy に必ず含める文言の要素:
- `day 2 以降`
- `1 巡目`
- `前夜`
- `能力結果`
- `誤爆リスク`
- `白先が本物の狼とは限らない`
- `処刑なしの日は結果なし`
- `合法な護衛履歴`

## 5. 共通の偽結果整合ルールを必要最小限で補強する

必要なら `_build_game_rules_block()` の既存の偽 CO ルールに、以下を短く追加してよい。

必要な仕様:
- 人狼・狂人が偽 CO する場合でも、結果の対象・日付・白黒・護衛履歴はこの bot の実ルール上あり得る内容にする。
- day 2 以降に CO 中の占い師・霊媒師・騎士は、真でも偽でも、昼 1 巡目で前夜相当の結果を出すのが信用上重要である。
- 偽結果は、公開ログ、処刑者、死亡者、過去の自分の結果、対抗結果と矛盾させない。

注意:
- 共通ルールに入れる場合、人狼専用の `相方` や襲撃計画は書かない。
- 共通ルールは全役職が見るので、role-specific な戦術詳細は werewolf / madman strategy に置く。
- 既存の `NIGHT_0` ランダム白、day 1 初回白、day 1 初回黒禁止は維持する。

## 6. 情報秘匿と role leak を壊さない

必要な仕様:
- 非人狼 prompt に人狼相方情報が混ざってはいけない。
- 狂人 prompt に `相方`、`襲撃先を揃える`、実狼位置を知っている前提の文面が混ざってはいけない。
- `task_daytime_speech()` に追加する 1 巡目結果公開ルールは、情報役 CO 一般の信用ルールとして書き、人狼専用連携語彙を入れない。
- `task_night_action(SEER_DIVINE)` の占い先選定軸には、人狼専用の襲撃判断語彙を入れない。
- `task_night_action(WOLF_ATTACK)` と `task_night_action(KNIGHT_GUARD)` の既存チェックリストを壊さない。
- `build_user_context()` の `wolf_partner_block` は既存どおり `me.role is Role.WEREWOLF` の場合だけ表示する。
- `LLMAdapter._ask()` の `game_id` / `audience_seat` スコープ分離を維持する。

やってはいけないこと:
- 配役を変える
- 勝利条件を変える
- 夜行動の合法候補を変える
- 夜の解決順を変える
- DB schema を変更する
- slash command を追加する
- Discord API から message history を直接 prompt に流す
- user context に他者の実役職や推定表を追加する
- 非狼 strategy に `相方` や `襲撃先を揃える` を入れる
- 狂人に本物の狼位置が見えている前提で書く
- 無関係な refactor を広げる

## 7. テストを追加 / 更新する

`tests/test_llm_prompt_builder.py`:
- `_build_strategy_block(Role.SEER)` に、高情報量の占い先、灰を狭める、対抗 CO、囲い候補、投票、白でも黒でも情報が落ちる、という趣旨が含まれること。
- `task_night_action(SubmissionType.SEER_DIVINE, ...)` に、占い価値、灰を狭める、対抗 CO、囲い候補、投票、白でも黒でも情報が落ちる、という趣旨が含まれること。
- `task_night_action(SubmissionType.SEER_DIVINE, ...)` に、wolf attack 専用の `襲撃価値`、`護衛されやすさ`、`騎士候補度`、`騎士探し`、`翌日の説明しやすさ` が含まれないこと。
- `task_daytime_speech(day_number=2, discussion_round=1)` に、`day 2 以降`、`1 巡目`、`前夜`、`能力結果`、占い師/霊媒師/騎士の結果添付が含まれること。
- `task_daytime_speech(day_number=1, discussion_round=1)` には day 2 以降の前夜結果必須指示が含まれないこと。
- `task_daytime_speech(day_number=2, discussion_round=2)` には `1 巡目` 必須指示が含まれないこと。
- `_build_strategy_block(Role.WEREWOLF)` に、day 2 以降、1 巡目、前夜、能力結果、相方、囲い、噛み筋、霊媒結果、合法な護衛履歴が含まれること。
- `_build_strategy_block(Role.MADMAN)` に、day 2 以降、1 巡目、前夜、能力結果、誤爆リスク、白先が本物の狼とは限らない、処刑なしの日は結果なし、合法な護衛履歴が含まれること。
- `_build_strategy_block(Role.MADMAN)` に `相方` / `襲撃先を揃える` が含まれないこと。
- 既存の role-specific cross-leak テストを維持し、新文面で必要な期待値を追加すること。

`tests/test_llm_service.py`:
- `_CapturingDecider` を使い、`DAY_DISCUSSION` round 1 の `_ask()` に渡る `system_prompt` / task text に、day 2 以降 1 巡目の前夜結果公開ルールが届くことを検証する。
- round 2 の `_ask()` では、前夜結果を「必ず添える」1 巡目指示が出ないことを検証する。
- `Role.SEER` の system prompt に、強化された占い先選定軸が届くことを検証する。
- `Role.WEREWOLF` の system prompt に、day 2 以降 1 巡目の偽結果発表と、相方・囲い・噛み筋整合の guidance が届くことを検証する。
- `Role.MADMAN` の system prompt に、day 2 以降 1 巡目の偽結果発表と誤爆リスクが届き、`相方` / `襲撃先を揃える` は入らないことを検証する。
- `task_night_action(SubmissionType.SEER_DIVINE, ...)` を task_text として渡したとき、system prompt に占い先選定軸が入ることを検証する。
- 非狼 prompt への人狼専用語彙漏れ、狂人への相方語彙漏れ、`game_id` / `audience_seat` スコープ分離の既存テストを壊さないこと。

既存テスト群は壊さないこと:
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`
- `tests/test_rules_night_targets.py`

実行する検証コマンド:
- `uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py tests/test_llm_structured_output.py tests/test_llm_resolver.py tests/test_llm_trigger.py tests/test_rules_night_targets.py`
- `uv run ruff check src tests`
- `uv run mypy`

## 8. 受け入れ条件

- 真占い師 LLM は、夜の占い対象を「白でも黒でも情報が落ちるか」「灰を狭めるか」「対抗 CO / 囲い候補 / 投票履歴の整理に効くか」で比較する prompt を受け取る。
- `SEER_DIVINE` の夜行動 task 自体にも、熟練者向けの占い先選定軸が入っている。
- day 2 以降の `DAY_DISCUSSION` 1 巡目で、占い師・霊媒師・騎士として CO 済みまたは CO 予定の LLM は、前夜相当の能力結果を発言に添えるよう促される。
- 人狼 LLM は、偽占い・偽霊媒・偽騎士の結果を、相方位置、囲いリスク、噛み筋、霊媒結果、投票、過去発言と矛盾しないよう選ぶ guidance を受け取る。
- 狂人 LLM は、偽結果を出すときに本物の人狼位置を知らない前提、黒誤爆リスク、白先誤認リスクを認識する。
- day 1 占い師騙りの初回白、day 1 初回黒禁止、day 2 以降の黒出し検討という既存仕様は維持される。
- 非狼 prompt に人狼相方情報や人狼専用連携語彙が漏れない。
- 狂人 prompt に `相方` / `襲撃先を揃える` が入らない。
- DB schema、domain rules、状態遷移、夜行動解決、Discord command は変更されていない。
```
