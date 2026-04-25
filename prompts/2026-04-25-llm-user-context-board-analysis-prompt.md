# `wolfbot` 2026-04-25 LLM User Context Board Analysis Update Prompt

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、LLM player の user context に、公開ログから機械的に整理した CO parser、自動盤面分類、縄数自動計算、役職推定結果を追加し、LLM player をより強い熟練した人狼プレイヤーとして動かすことです。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM player が、公開ログと自分に見えている私的情報を熟練者のように整理してから発言・投票・夜行動できるようにする。
- `build_user_context()` に、CO parser、自動盤面分類、縄数自動計算、役職推定結果を追加する。
- 追加する情報は、LLM の判断補助として prompt に渡す派生情報であり、ゲームルール・勝利条件・状態遷移・DB schema は変えない。

今回必ず対応すること:
1. 公開ログから、占い師 / 霊媒師 / 騎士の CO と、占い結果・霊媒結果・護衛主張を保守的に抽出する CO parser を追加すること。
2. 抽出した CO 数から、`3-1`、`2-2`、`2-1`、`1-2`、`1-1`、`その他/未分類` などの盤面を自動分類し、user context に出すこと。
3. 現在の生存人数から縄数を自動計算し、残り処刑回数、開始時 4 縄、PP / RPP リスクの目安を user context に出すこと。
4. 各 seat について、公開情報ベースの役職推定メモを user context に出すこと。
5. 役職推定は実役職を漏らすものではなく、公開ログと本人に許された私的情報からの保守的な推定に限定すること。
6. 既存の system prompt、role-specific strategy、persona 話法、structured output 制約、情報秘匿、`game_id` / `audience_seat` 分離を壊さないこと。

最重要ルール:
- まず既存実装を読み、現在の prompt 構築、LLM 呼び出し、ログ永続化、テスト構成を把握してから修正すること。
- `domain/` は純粋ロジックのまま保つ。Discord I/O、xAI I/O、SQLite I/O を domain に入れないこと。
- Discord channel history を直接読んで prompt に入れてはいけない。LLM 文脈は既存どおり DB の public/private logs からだけ構築すること。
- DB schema は変更しないこと。CO 整理・盤面分類・縄数・役職推定は、`build_user_context()` 呼び出し時にメモリ上で派生計算すること。
- slash command は追加しないこと。
- 9 人村固定配役、勝利条件、状態遷移、投票処理、夜行動処理、recovery、permission 管理は変えないこと。
- 非狼の prompt に狼相方情報を漏らしてはいけない。
- 狂人に本物の人狼位置が見えている前提の情報を渡してはいけない。
- 役職推定で `Player.role` を実役職として表示してはいけない。ただし、本人の役職、狼本人から見える仲間、人狼専用 private log、占い/霊媒/騎士本人に送られた private result など、既存で本人に見えている情報は既存どおり扱ってよい。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/prompts/llm_system_prompt.md`
- `src/wolfbot/llm/prompt_builder.py`
- `src/wolfbot/llm/personas.py`
- `src/wolfbot/services/llm_service.py`
- `src/wolfbot/domain/enums.py`
- `src/wolfbot/domain/models.py`
- `src/wolfbot/domain/rules.py`
- `src/wolfbot/domain/state_machine.py`
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`

このリポジトリで確認済みの事実:
- runtime LLM template は `src/wolfbot/prompts/llm_system_prompt.md`。
- system prompt は `src/wolfbot/llm/prompt_builder.py::build_system_prompt()` が合成する。
- user context は `src/wolfbot/llm/prompt_builder.py::build_user_context()` が作る。
- `LLMAdapter._ask()` は `repo.load_public_logs(game.id, limit=40)` と `repo.load_private_logs_for_audience(game.id, audience_seat=player.seat_no, limit=40)` を使う。
- `build_user_context()` は現在、生存者、死亡者、現在フェイズ、狼本人だけに見える仲間情報、私的メモ、公開ログ、自分の直近発言を返している。
- 公開ログの `PLAYER_SPEECH` は `actor_seat` 付きで保存され、`build_user_context()` は `席N display_name: text` 形式で speaker を補っている。
- 人狼相方情報は `me.role is Role.WEREWOLF` の場合だけ user context に入る。
- system prompt の共通ルールには、既に `3-1` / `2-2` / `2-1` / `1-2`、縄計算、PP / RPP、CO 履歴、判定履歴、投票履歴、噛み筋などの推理語彙が含まれている。
- ただし現状の user context には、公開ログから機械抽出した CO 表、盤面分類、縄数、役職推定メモはない。

実装方針:
- 新しい文脈解析は `src/wolfbot/llm/` 配下に閉じる。
- 推奨ファイルは `src/wolfbot/llm/context_analysis.py`。
- `context_analysis.py` は純粋関数だけにし、DB、Discord、OpenAI / xAI client、asyncio に依存させない。
- `prompt_builder.py` は `context_analysis.py` の結果を Markdown ブロックとして user context に差し込む。
- 既存の `build_system_prompt()` の公開引数は変えない。
- `build_user_context()` の引数はできるだけ維持する。必要なら内部 helper を追加するだけにする。
- 解析は不完全でよいが、誤検出時に強く断定しない文面にする。

## 1. CO parser を追加する

必要な仕様:
- 入力は `public_logs: Sequence[dict[str, object]]`、`seats: Sequence[Seat]`、必要なら `private_logs`。
- 主対象は public log の `kind == "PLAYER_SPEECH"`。
- `actor_seat` がある発言だけを、誰の CO / 判定かに紐付ける。
- `actor_seat` がない public log は、CO parser の対象外にするか、低信頼の材料として扱う。
- 文字列 parser は保守的にする。曖昧な文は「未分類」へ逃がし、強い確定情報として扱わない。
- 日本語の表記ゆれを最低限拾う。

抽出対象:
- 占い師 CO:
  - `占いCO`
  - `占い師CO`
  - `占い師です`
  - `占いです`
  - `占い師として出る`
- 霊媒師 CO:
  - `霊媒CO`
  - `霊媒師CO`
  - `霊媒師です`
  - `霊媒です`
- 騎士 CO:
  - `騎士CO`
  - `狩人CO`
  - `騎士です`
  - `狩人です`
  - この bot の役職名は `騎士` だが、人狼用語として `狩人` も同義扱いしてよい。
- 占い結果:
  - `席N name 白`
  - `席N name 黒`
  - `name 白`
  - `name 黒`
  - `占った`
  - `人狼ではない`
  - `人狼だった`
  - `黒判定`
  - `白判定`
- 霊媒結果:
  - `霊媒結果`
  - `処刑された X は白`
  - `処刑された X は黒`
  - `X は人狼ではありませんでした`
  - `X は人狼でした`
- 護衛主張:
  - `護衛`
  - `守った`
  - `GJ`
  - `平和`
  - `護衛先`

出力モデル:
- `context_analysis.py` に frozen dataclass か Pydantic frozen model を追加する。
- 型は strict mypy で通るように完全にアノテーションする。
- 推奨モデル:
  - `ClaimedRole(actor_seat: int, role: Role, day: int, raw_text: str)`
  - `ClaimedResult(actor_seat: int, target_seat: int | None, kind: Literal["SEER", "MEDIUM", "GUARD"], result: Literal["WHITE", "BLACK", "GUARD", "GJ"] | None, day: int, raw_text: str)`
  - `ContextAnalysis(claimed_roles: tuple[ClaimedRole, ...], claimed_results: tuple[ClaimedResult, ...], board_label: str, rope_summary: str, role_estimates: tuple[RoleEstimate, ...])`
  - `RoleEstimate(seat_no: int, display_name: str, alive: bool, public_claims: tuple[str, ...], public_status: str, confidence: Literal["low", "medium", "high"])`
- `Role` enum は既存の `wolfbot.domain.enums.Role` を使う。
- parser が `狩人` を見つけた場合は `Role.KNIGHT` として扱う。

候補解決:
- `席N` がある場合は seat number を優先する。
- `席N` がない名前だけの言及は、display_name が一意に一致するときだけ target_seat に解決する。
- display_name が重複、部分一致のみ、または曖昧なら `target_seat=None` にする。
- LLM persona display_name には emoji が含まれる可能性があるため、完全一致と seat token を優先する。

## 2. 自動盤面分類を追加する

必要な仕様:
- CO parser の `claimed_roles` から、占い師 CO 数、霊媒師 CO 数、騎士 CO 数を集計する。
- 同じ seat が同じ role を複数回 CO しても 1 人として数える。
- 同じ seat が複数 role を CO した場合は、矛盾 CO として両方の候補に残し、役職推定で「矛盾 CO」扱いにする。
- 死亡済み CO 者も CO 数に含める。現在生存している CO 者だけで盤面分類しない。
- 盤面分類は公開ログ上の CO 履歴であり、真役職数ではないと明記する。

分類ラベル:
- 占い師 CO 3 / 霊媒師 CO 1: `3-1`
- 占い師 CO 2 / 霊媒師 CO 2: `2-2`
- 占い師 CO 2 / 霊媒師 CO 1: `2-1`
- 占い師 CO 1 / 霊媒師 CO 2: `1-2`
- 占い師 CO 1 / 霊媒師 CO 1: `1-1`
- 占い師 CO 0 / 霊媒師 CO 0: `CO なし/未展開`
- 上記以外: `その他/未分類`

user context 表示例:

```text
## CO・判定の機械整理
- 占い師CO: 席2 セツ, 席5 Alice
- 霊媒師CO: 席4 Bob
- 騎士CO: (なし)
- 判定主張: 席2 セツ -> 席7 Gina 白 / 席5 Alice -> 席3 Raqio 黒

## 盤面分類
- 公開CO履歴ベース: 2-1
- 注意: これは真役職数ではなく、公開発言から抽出したCO数です。死亡済みCO者も含みます。
```

文面ルール:
- 「確定」「真確定」などの断定は避ける。
- `単独COは真寄り` などの判断方針は system prompt の既存共通ルールに任せ、user context では事実整理に寄せる。

## 3. 縄数自動計算を追加する

必要な仕様:
- 現在の生存人数から残り縄数を計算する。
- 標準目安は `floor((alive_count - 1) / 2)`。
- 9 人村開始時は 4 縄と表示する。
- 死亡人数、残り生存人数、残り縄数を user context に出す。
- PP / RPP リスクは過度に断定せず、注意喚起として表示する。
- 実際の残り人狼数や狂人生存は、公開情報から確定できない限り断定しない。

実装方針:
- `alive_count = len([p for p in players if p.alive])`
- `dead_count = len(players) - alive_count`
- `ropes_left = max(0, (alive_count - 1) // 2)`
- `starting_ropes = 4`
- `used_executions` は厳密に取れない場合、死亡者数とは別に扱う。公開ログの処刑ログを parse して取れるなら低リスクで追加してよいが、必須ではない。

user context 表示例:

```text
## 縄数・PP/RPPリスク
- 生存 7 人 / 死亡 2 人。残り処刑回数の目安: 3 縄 (9人村開始時は4縄)。
- 注意: 残り人狼数と狂人生存は公開情報から推定する必要があります。終盤はPP/RPPの可能性を確認してください。
```

PP / RPP 目安:
- 生存 5 人以下では PP / RPP 注意を強める。
- 生存 3 人では最終局面として扱う。
- ただし「PP 確定」とは書かない。公開情報から確定できないため。

## 4. 役職推定結果を追加する

必要な仕様:
- 各 seat について、公開情報上の状態を短くまとめる。
- 実役職を DB の `Player.role` から出してはいけない。
- 役職推定は、公開 CO、判定主張、生死、単独 / 対抗あり、矛盾 CO、パンダ、灰などの整理に留める。
- LLM が発言時に「誰をどう見るか」を始めやすいように、熟練者のメモ風にする。
- ただし長い内部推論を user context に押し込みすぎない。各 seat 1 行程度にする。

推定ラベル例:
- `占い師CO`
- `霊媒師CO`
- `騎士CO`
- `複数CO/矛盾CO`
- `灰`
- `白もらい`
- `黒もらい`
- `パンダ`
- `死亡済みCO`
- `単独CO`
- `対抗あり`
- `公開情報少`

confidence の考え方:
- `high`: 公開ログ上の事実整理として強い。例: 自分で CO している、複数発言で同じ CO をしている。
- `medium`: 判定主張や CO 数から一定の整理ができる。
- `low`: 発言が少ない、曖昧、名前解決ができない、推定材料が薄い。

user context 表示例:

```text
## 役職推定メモ (公開情報ベース)
- 席1 Alice: 灰 / 判定なし / confidence=low
- 席2 セツ: 占い師CO / 対抗あり / 席7へ白主張 / confidence=high
- 席3 Raqio: 黒もらい / COなし / confidence=medium
- 席4 Bob: 単独霊媒師CO / confidence=high
```

注意文を必ず添える:
- `このメモは公開ログからの機械整理であり、真役職や本当の陣営を保証しません。自分に見えていない役職・狼位置を事実として断言しないでください。`

## 5. private log の扱い

必要な仕様:
- 既存の `private_logs` はそのまま `## あなたの私的メモ` に表示する。
- CO parser と盤面分類の基本材料は public logs に限定する。
- ただし、本人だけが知る占い結果・霊媒結果・護衛結果・wolf chat は既存どおり private log として user context に含まれるため、LLM はそれを自分の私的情報として使ってよい。
- private log を全体の「公開CO数」や「公開盤面分類」に混ぜないこと。
- 狼本人の相方情報 block は既存どおり `me.role is Role.WEREWOLF` の場合だけ表示する。

## 6. `build_user_context()` の表示順を更新する

推奨表示順:
1. 自分の座席
2. 生存者 / 死亡者
3. 現在フェイズ
4. 人狼本人だけの仲間情報 block
5. `## CO・判定の機械整理`
6. `## 盤面分類`
7. `## 縄数・PP/RPPリスク`
8. `## 役職推定メモ (公開情報ベース)`
9. `## あなたの私的メモ (他者には非公開)`
10. `## 公開ログ要約 (直近)`
11. `## 自分の直近の発言`

理由:
- LLM がログ本文を読む前に、機械整理された盤面と縄数を見られるようにする。
- ただし私的情報は既存どおり明確に「他者には非公開」と区切る。

## 7. 実装詳細

推奨 helper:
- `analyze_context(game, me, my_seat, seats, players, public_logs, private_logs) -> ContextAnalysis`
- `parse_claims(public_logs, seats) -> tuple[ClaimedRole, ...]`
- `parse_results(public_logs, seats) -> tuple[ClaimedResult, ...]`
- `classify_board(claimed_roles) -> BoardClassification`
- `calculate_rope_summary(players) -> RopeSummary`
- `estimate_public_roles(seats, players, claimed_roles, claimed_results) -> tuple[RoleEstimate, ...]`
- `render_context_analysis(analysis) -> str`

設計制約:
- `context_analysis.py` は public API を小さく保つ。
- regex はモジュール定数として定義してよい。
- parse できない発言を例外にしない。失敗時は空の解析結果で続行する。
- prompt 構築中の例外で LLM action 全体を落とさない。必要なら `build_user_context()` 側で保守的に空 block を返す。
- ただし通常テストで例外が隠れすぎないよう、純粋 helper の単体テストは直接書く。

## 8. テストを追加 / 更新する

`tests/test_llm_prompt_builder.py`:
- `build_user_context()` が `## CO・判定の機械整理` を含むこと。
- 占い師 CO、霊媒師 CO、騎士 CO を public `PLAYER_SPEECH` から抽出できること。
- 占い結果の白 / 黒を抽出し、seat token または一意な display_name から target seat を解決できること。
- display_name が曖昧な場合は target を断定しないこと。
- `2-1`、`3-1`、`2-2`、`1-2` などの盤面分類が出ること。
- 生存人数 9 / 7 / 5 / 3 で縄数がそれぞれ 4 / 3 / 2 / 1 になること。
- 役職推定メモに `灰`、`白もらい`、`黒もらい`、`パンダ`、`単独CO`、`対抗あり` の代表ケースが出ること。
- 役職推定メモに DB の実役職を根拠なく表示しないこと。

`tests/test_llm_service.py`:
- `_CapturingDecider` を使い、`LLMAdapter._ask()` が作った `user_context` に CO 整理、盤面分類、縄数、役職推定メモが届くことを確認する。
- 別 game の public/private log が解析 block に混ざらないことを確認する。
- 非狼の user context に `仲間の人狼` block が出ない既存保証を維持する。
- 狼の user context には既存どおり仲間情報が出るが、公開盤面分類には混ぜないことを確認する。

追加してよいテストファイル:
- `tests/test_llm_context_analysis.py`

このテストファイルを追加する場合:
- pure helper を直接テストし、DB や async を使わない。
- `Seat` / `Player` / fake public log dict を最小限で組み立てる。
- 文字列断片テストは高シグナルな語句に絞る。

既存テスト群は壊さないこと:
- `tests/test_llm_prompt_builder.py`
- `tests/test_llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`

## 9. 受け入れ条件

- LLM user context に、CO・判定の機械整理、盤面分類、縄数、役職推定メモが表示される。
- `3-1` / `2-2` / `2-1` / `1-2` の基本的な CO 盤面が、公開ログの CO 数から自動分類される。
- 残り縄数が生存人数から自動計算され、PP / RPP 注意が出る。
- 役職推定は公開情報ベースで、実役職・狼位置・秘匿情報を非公開 seat に漏らさない。
- parser は曖昧な名前や曖昧な発言を断定しない。
- `LLMAdapter._ask()` の `game_id` / `audience_seat` スコープ分離が維持される。
- DB schema、状態遷移、Discord command、ゲームルールは変わらない。
- mypy strict、ruff、関連 pytest が通る。

## 10. 検証コマンド

最低限:

```bash
uv run pytest tests/test_llm_context_analysis.py tests/test_llm_prompt_builder.py tests/test_llm_service.py
uv run ruff check src tests
uv run mypy
```

`tests/test_llm_context_analysis.py` を追加しない場合:

```bash
uv run pytest tests/test_llm_prompt_builder.py tests/test_llm_service.py
uv run ruff check src tests
uv run mypy
```

可能なら関連範囲も走らせる:

```bash
uv run pytest tests/test_llm_structured_output.py tests/test_llm_resolver.py tests/test_llm_trigger.py
```

最後に簡潔に報告すること:
- 追加した解析 helper と user context block
- 情報漏洩を防ぐために維持した境界
- 実行したテスト / lint / 型チェックと結果
```
