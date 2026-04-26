# `wolfbot` 2026-04-26 LLM 投票偏り抑制・熟練投票判断プロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player の投票先が候補順・席番号・名前印象・直近発言量・不正 target fallback などで不自然に偏る挙動を減らし、公開情報を根拠に投票先を選ぶ強い熟練した人狼プレイヤーへ近づけることです。

重要: 投票を人工的に均等化することが目的ではありません。公開情報上、特定候補が最も黒いなら複数 LLM の票が集まるのは自然です。減らしたいのは、根拠が薄いのに同じ候補へ寄る、候補リスト先頭へ寄る、invalid target から random fallback へ流れて投票理由と結果がずれる、という種類の偏りです。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player を、投票判断においてより強い熟練した人狼プレイヤーとして振る舞わせる。
- LLM player が候補順、席番号、名前の目立ちやすさ、直近発言量、不正 `target_name` の random fallback に引きずられて同じ投票先へ偏る挙動を減らす。
- ただし、公開情報上その候補が最も黒い場合の自然な集票は残す。票を機械的に分散させる改変はしない。
- 投票ルール、投票解決、同票/決選処理、勝利条件、Discord UI、DB schema は変更しない。

今回必ず対応すること:
1. `task_vote()` の共通投票 task に、候補順で選ばず、CO 履歴・判定履歴・投票履歴・発言矛盾・縄数・2 人狼仮説で比較して投票するルーブリックを追加すること。
2. LLM に「複数人が同じ候補へ投票すること自体は悪ではなく、根拠が揃っているなら自然」と明記すること。
3. LLM 向けの投票候補 token 表示順を、投票者・day・round ごとに deterministic pseudo-shuffle し、候補リスト先頭 bias を減らすこと。
4. pseudo-shuffle は LLM prompt 内の候補提示順だけに適用し、人間 DM の表示順、投票候補集合、投票集計、決選候補、状態遷移は変えないこと。
5. user context に、公開済み投票結果から作る短い「投票履歴メモ」を追加し、LLM が票筋を読みやすくすること。
6. 現在進行中の未公開投票状況、途中経過、未投票者以外の投票先は絶対に LLM prompt に渡さないこと。
7. 投票時の `target_name` が候補 token に解決できない場合は、投票についてだけ 1 回まで補正再依頼を検討し、それでも不正なら既存の random fallback へ落とすこと。
8. 人狼専用の相方情報・身内票・ライン切り guidance は既存どおり人狼本人にだけ渡し、狂人・村役職・村人へ漏らさないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築、LLM 投票フロー、target resolver、公開ログ永続化、既存テストを把握してから修正すること。
- `domain/` は純粋ロジックのまま保つ。今回の変更で `compute_vote_result()`、投票解決、同票/決選処理、勝敗判定を変えない。
- Discord channel history を直接読んで prompt に入れてはいけない。LLM 文脈は既存どおり DB の public/private logs からだけ構築すること。
- 現在の未公開投票状況を LLM に見せてはいけない。投票済み/未投票や投票先の途中経過を新しく prompt に渡さない。
- 投票をコード側で均す、特定候補を自動除外する、票を強制的に散らす、過去投票先を理由なく避けさせる、といった補正はしない。
- `LLMAction` schema は変更しない。`target_name` は引き続き候補 token と完全一致させる。
- 既存の persona 話法、structured output、fire-and-forget LLM vote dispatch、stale phase/day guard を壊さない。
- 実装後は必ず関連 pytest、ruff、mypy を走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/domain/state_machine.py`
- `src/wolfbot/domain/rules.py`
- `src/wolfbot/persistence/sqlite_repo.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_structured_output.py`
- `tests/test_state_machine_votes.py`
- `tests/test_rules_votes.py`

このリポジトリで確認済みの事実:
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md`。
- system prompt は `src/wolfbot/llm/prompt_builder.py::build_system_prompt()` が合成する。
- user context は `src/wolfbot/llm/prompt_builder.py::build_user_context()` が作る。
- 投票 task は `src/wolfbot/llm/prompt_builder.py::task_vote()` が作る。
- LLM 投票は `src/wolfbot/services/llm_service.py::LLMAdapter._run_votes()` が `task_vote()` を渡して `_ask()` し、`target_name` を候補 token から解決して `GameService.submit_vote()` に渡す。
- 候補 token は `src/wolfbot/services/llm_service.py::seat_token()` で `席{N} {display_name}` として作る。
- `_resolve_target()` は seat token を優先し、bare name は一意なら受け、解決不能なら random fallback する。
- `build_user_context()` は DB の `public_logs` と `private_logs` から文脈を組み立てる。Discord channel history を直接読まない。
- 人狼相方情報は `build_user_context()` と `task_vote()` の wolf-only block で人狼本人にだけ渡されている。
- 共通ルールには既に `票筋`、`ライン`、`身内切り`、`グレスケ`、`縄計算`、`PP/RPP`、2 人狼仮説などの語彙がある。
- 投票結果の公開ログは `domain/state_machine.py` 側で「投票者 -> 投票先」形式になっており、公開ログとして LLM user context に届く。

実装方針:
- まずは prompt 強化 + 候補表示順 bias の抑制 + 公開済み投票履歴メモで対応する。
- 新しい DB カラム、Discord UI、投票 resolver の domain 変更、vote outcome 補正は追加しない。
- `build_system_prompt()` の公開引数は変えない。
- `LLMAction` schema は変えない。
- `task_vote()` は既存 optional 引数 (`role`, `wolf_partner_tokens`) と互換性を保つ。
- 変更範囲は原則として `src/wolfbot/llm/prompt_builder.py`、`src/wolfbot/services/llm_service.py`、関連 tests に閉じる。

## 1. `task_vote()` の共通投票ルーブリックを強化する

`src/wolfbot/llm/prompt_builder.py::task_vote()` の base text を更新すること。

必須内容:
- 投票先は候補リストの先頭、席番号の若さ/大きさ、名前の印象、直近に目立った発言だけで選ばない。
- 以下を比較して、最も黒い、または今日処理する価値が高い候補を選ぶ。
  - CO 履歴: 誰がいつ何を CO したか、対抗の有無、単独 CO か contested CO か。
  - 判定履歴: 占い/霊媒の白黒、誰視点の結果か、狂人白ルールとの整合。
  - 投票履歴: 過去に誰が誰へ投票したか、同票、決選、票変え、身内票/ライン切りの可能性。
  - 発言: 矛盾、便乗、根拠の薄い誘導、視点漏れ、急な疑い先変更。
  - 噛み筋: 情報役噛み、白位置噛み、意見噛み、残された CO 者の不自然さ。
  - 縄数: 今日ミスできるか、PP/RPP が近いか。
  - 2 人狼仮説: その候補が人狼なら相方候補は誰か、票筋・庇い・距離感が自然か。
- 複数 LLM が同じ候補に投票すること自体は問題ではない。公開情報上その候補が最黒なら自然な集票として扱う。
- 逆に、理由が薄いのに候補順や名前印象だけで同じ候補へ寄るのは避ける。
- 投票理由は内部で比較し、`reason_summary` に短く残す。公開発言のような長文は不要。
- 最終的には必ず合法候補 token から 1 名を返す。棄権は、公開情報上どうしても投票先を決められない場合だけにする。

推奨文面例:

```text
- 候補リストの先頭、席番号、名前の印象、直近で目立った発言だけで投票先を決めないでください。
- CO 履歴、判定履歴、過去の投票履歴、発言矛盾、噛み筋、縄数、2 人狼仮説を比較し、今日もっとも処理価値が高い候補を選んでください。
- 複数人の票が同じ候補に集まること自体は悪ではありません。公開情報上その候補が最黒なら自然な集票です。避けるべきなのは、根拠が薄いのに候補順や雰囲気で同じ相手へ寄ることです。
```

注意:
- `task_vote()` の human-facing UI には影響させない。
- 人狼専用 block は既存の `role is Role.WEREWOLF` gate を維持する。
- 共通 text に `仲間の人狼` や実際の相方を知っている前提の文面を入れない。

## 2. LLM vote candidate tokens の提示順だけを deterministic pseudo-shuffle する

`src/wolfbot/services/llm_service.py::LLMAdapter._run_votes()` の LLM 向け `task_vote()` 呼び出しで、候補 token の順序を voter/day/round ごとに決定的に並べ替えること。

目的:
- `candidate_tokens` が seat 順のままだと、LLM が先頭候補や同じ位置の候補を選びやすい。
- ゲーム挙動は変えず、LLM prompt 内の表示順だけを分散する。
- 同じ game/day/round/voter/candidate set なら何度再送しても同じ順にする。recovery や `/wolf extend` で候補順が揺れないようにする。

推奨実装:
- `LLMAdapter` 内に private helper を追加する。
- 入力は `game.id`, `game.day_number`, `round_`, `voter.seat_no`, `cand_seats`。
- 各 candidate seat について、例えば `hashlib.sha256(f"{game_id}:{day}:{round_}:{voter_seat}:{candidate_seat}".encode()).hexdigest()` を key にして sort する。
- `random.Random` を使ってもよいが、その場合も game/day/round/voter から作る固定 seed にし、adapter の共有 `self.rng` 状態を消費しない。
- `cand_seats` の集合は変えず、順序だけ変える。

必須内容:
- 人間 DM の候補順、`send_vote_dms()`、`GameService.submit_vote()`、`compute_vote_result()`、`plan_day_vote_resolve()` は変えない。
- 決選投票 (`round_=1`) でも同じ helper を使う。
- `restrict_to_seats` による再ディスパッチでも同じ voter には同じ順序を出す。
- pseudo-shuffle 後も `target_name` は `seat_token()` で作る合法候補 token と完全一致させる。
- 候補が 0 件または 1 件の場合はそのままでよい。

テスト観点:
- 同じ `game_id/day/round/voter/candidates` なら順序が再現する。
- `voter_seat` が違うと順序が変わり得る。
- pseudo-shuffle 後の seat 集合は元の候補集合と完全一致する。
- `round_=0` と `round_=1` で seed が分離される。

## 3. 公開済み投票履歴メモを user context に追加する

`build_user_context()` に、公開済み投票履歴だけを短く整理する block を追加すること。

目的:
- LLM が raw public log の中から投票結果を探すだけだと、票筋を十分に使えず、発言印象や候補順に寄りやすい。
- 熟練者が見る「誰が誰へ投票したか」「同票・決選・票変えがあったか」を、公開済み情報の範囲で読みやすくする。

必須内容:
- block 名は `## 公開済み投票履歴メモ` などにする。
- 対象は公開ログに既に出た投票結果だけ。現在進行中の投票途中経過は含めない。
- 現在 `DAY_VOTE` 中の round 0 partial vote は含めない。
- 現在 `DAY_RUNOFF` / `DAY_RUNOFF_SPEECH` 中は、通常投票の結果が公開ログに出ている場合だけ round 0 の情報を含めてよい。
- 過去 day の通常投票・決選投票は公開済みなら含めてよい。
- private logs や DB votes の未公開状態から直接「途中投票結果」を作らない。

実装選択肢:
- 最小実装では、`public_logs` の `EXECUTION`, `RUNOFF_START`, `NO_EXECUTION` などに含まれる `🗳 投票結果:` block を抽出し、直近数件だけ user context に再掲する。
- より構造化する場合でも、DB の `votes` を現在進行中 phase の partial state として読むのではなく、公開ログに出た範囲に限定する。
- 解析 helper は `src/wolfbot/llm/prompt_builder.py` 内の小さな private function でよい。大きな parser や新規 schema は不要。

推奨出力例:

```text
## 公開済み投票履歴メモ
- day 1 通常投票: 席1 A -> 席7 G / 席2 B -> 席7 G / 席3 C -> 棄権
- day 1 決選投票: 席1 A -> 席7 G / 席2 B -> 席6 F
```

注意:
- 投票履歴メモは公開済み情報の再整理であり、役職や未公開投票を推定してはいけない。
- prompt 構築中の例外で LLM action 全体を落とさない。抽出に失敗したら block は `(公開済み投票履歴なし)` でよい。
- block が長くなりすぎないよう、直近 3〜5 件程度に制限する。

## 4. invalid `target_name` の投票 fallback を減らす

現在 `_resolve_target()` は解決不能な `target_name` を random fallback する。これは安全策として必要だが、投票理由と実際の投票先がずれ、偏りや不自然な票を生む可能性がある。

必要な仕様:
- 投票 (`intent=vote`) についてだけ、`target_name` が候補 token に解決できない場合、1 回まで補正再依頼を試みる。
- 補正再依頼でも不正なら、既存どおり random fallback へ落としてゲーム進行を止めない。
- 夜行動は今回の主眼ではないため、既存挙動を無理に変えない。
- `intent=skip` はこれまでどおり skip を優先し、`target_name` に junk があっても投票へ変換しない。

推奨実装:
- `_run_votes()` の `_one_vote()` 内で、最初の action が `intent != "skip"` かつ target 解決不能かを検出する。
- `_resolve_target()` は現状 random fallback を内包しているため、補正再依頼を実装しやすくするには、private helper を分ける。
  - 例: `_try_resolve_target(target_name, candidates, allow_none) -> int | None | _UNRESOLVED`
  - `_resolve_target()` は互換 wrapper として残し、最終 fallback だけを担当する。
- 補正再依頼の task text には、合法候補 token 一覧を再掲し、`target_name` をその中の 1 つに完全一致させるよう短く伝える。
- 補正再依頼は最大 1 回。無限 retry はしない。

注意:
- DeepSeek / Gemini / xAI の structured output 契約は変えない。
- `LLMAction` に新しい field を足さない。
- 補正再依頼に失敗しても per-seat vote task 全体を落とさず、既存 fallback で合法候補へ収める。
- random fallback は最後の安全策として残すが、warning log の文脈で補正失敗が分かるようにするとよい。

## 5. 情報秘匿と role leak を壊さない

必ず守ること:
- 非人狼 prompt に `仲間の人狼`、実際の相方 token、実際の相方を救う/切る指示を出さない。
- 狂人は本物の人狼位置を知らないため、狂人 prompt に実際の相方情報を渡さない。
- 共通ルールの `相方候補`、`2 人狼仮説`、`身内票`、`ライン切り` は公開ログを読むための推理語彙として残してよい。
- 人狼本人にだけ、既存の `wolf_partner_tokens` を使った相方投票 guidance を出す。
- 投票履歴メモは公開済みの票筋だけで、実役職・未公開票・夜行動情報を混ぜない。
- `game_id` / `audience_seat` のスコープ分離を維持する。

やってはいけないこと:
- 投票 resolver や `compute_vote_result()` を変更して票を補正する。
- LLM の投票先を code 側で強制的に分散させる。
- 候補順 pseudo-shuffle を人間 DM や Discord UI に適用する。
- 現在の未公開投票状況を prompt に渡す。
- 投票先候補から過去投票先や相方を自動除外する。
- 公開ログ parser を大規模に作り直す。
- DB schema、Discord command、DM vote UI を変更する。
- `LLMAction` schema を変更する。
- 無関係な refactor。

## 6. テストを追加 / 更新する

`tests/test_llm_prompt_builder.py`:
- `task_vote(["席1 A", "席2 B"], runoff=False)` に、候補順・席番号・名前印象だけで選ばない趣旨が含まれること。
- `task_vote()` に、CO 履歴、判定履歴、投票履歴、発言矛盾、縄数、2 人狼仮説を比較する趣旨が含まれること。
- `task_vote()` に、根拠がある場合の自然な集票は許容する趣旨が含まれること。
- 通常の `task_vote()` には `仲間の人狼` や実際の相方 token が含まれないこと。
- `task_vote(role=Role.WEREWOLF, wolf_partner_tokens=[...])` の既存 wolf-only guidance が維持されること。
- `Role.MADMAN` や `Role.VILLAGER` に partner token を渡しても、`仲間の人狼` が出ないこと。
- `build_user_context()` に `公開済み投票履歴メモ` block が出ること。
- 公開ログに投票結果がない場合は、投票履歴メモが空または `(公開済み投票履歴なし)` になること。
- 公開ログの `🗳 投票結果:` block から、直近投票履歴が短く抽出されること。

`tests/test_llm_service.py`:
- LLM vote candidate order pseudo-shuffle が同じ `game_id/day/round/voter/candidates` で deterministic であること。
- `voter_seat` または `round_` が変わると順序が変わり得ること。
- pseudo-shuffle 後も候補集合が変わらないこと。
- `_run_votes()` が pseudo-shuffle 後の token 順で `task_vote()` を作ることを `_CapturingDecider` などで確認すること。
- 人間 DM 送信側には pseudo-shuffle を適用しない既存挙動を壊さないこと。
- 人狼 voter の vote prompt には既存どおり `仲間の人狼`、`身内票`、`ライン切り` が届くこと。
- 非人狼 voter の vote prompt に実際の partner token が漏れないこと。
- invalid `target_name` の補正再依頼を実装した場合、1 回だけ再依頼し、2 回目で合法候補ならその候補が採用されること。
- 補正再依頼後も不正なら、最終的に合法候補内へ fallback すること。
- `intent=skip` は junk `target_name` があっても棄権として扱われ、random fallback 投票へ変換されないこと。

`tests/test_llm_resolver.py`:
- 分離した `_try_resolve_target()` または同等 helper が、合法 seat token、unique bare name、ambiguous name、unknown token、null を期待どおり扱うこと。
- 既存 `_resolve_target()` の fallback 互換性が維持されること。

`tests/test_state_machine_votes.py` / `tests/test_rules_votes.py`:
- 投票集計、同票、決選、棄権、self-vote 無効化の既存挙動が変わっていないこと。
- 投票結果表示の既存テストが壊れないこと。

既存テスト群は壊さないこと:
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_structured_output.py`
- `tests/test_rules_votes.py`
- `tests/test_state_machine_votes.py`
- `tests/test_game_service_advance.py`

## 7. 受け入れ条件

- LLM vote prompt が、候補順・席番号・名前印象に流されず、公開情報を比較して投票するよう明確に指示している。
- 根拠がある場合の自然な集票は許容され、機械的な票分散は指示されていない。
- LLM 向け投票候補 token の提示順だけが deterministic pseudo-shuffle され、投票候補集合や投票解決は変わらない。
- user context に、公開済み投票履歴メモが表示され、LLM が票筋を読みやすくなっている。
- 現在進行中の未公開投票状況や途中投票先は prompt に入らない。
- invalid `target_name` による random fallback が投票時に減り、補正再依頼後も最終的には合法候補内に収まる。
- 非人狼 LLM と狂人 LLM に、実際の人狼相方情報や人狼専用投票 guidance が漏れない。
- 投票ルール、投票候補、投票解決、同票/決選処理、DB schema、Discord UI、structured output schema は変わらない。

## 8. 検証コマンド

最低限:

```bash
uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py tests/test_llm_resolver.py tests/test_state_machine_votes.py tests/test_rules_votes.py
uv run ruff check src tests
uv run mypy
```

可能なら関連範囲も走らせる:

```bash
uv run pytest tests/test_llm_structured_output.py tests/test_game_service_advance.py tests/test_recovery.py
```

最後に簡潔に報告すること:
- `task_vote()` の投票ルーブリックをどう強化したか
- LLM 候補 token 表示順の pseudo-shuffle の実装概要
- 公開済み投票履歴メモの情報源と秘匿境界
- invalid `target_name` fallback をどう減らしたか
- 人狼専用情報が非狼 prompt に漏れないようにした境界
- 実行したテスト / lint / 型チェックと結果
- 残課題があればその内容
```
