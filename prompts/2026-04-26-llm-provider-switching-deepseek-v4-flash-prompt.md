# `wolfbot` 2026-04-26 LLM Provider Switching / DeepSeek V4 Flash Update Prompt

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、既存の xAI Grok 4.1 Fast 前提を壊さずに、DeepSeek V4 Flash を thinking mode / max effort で使えるようにし、必要に応じて Grok と DeepSeek を環境変数で切り替えられるようにすることです。

調査済みの一次情報:
- DeepSeek V4 Preview Release: https://api-docs.deepseek.com/news/news260424
- DeepSeek Thinking Mode: https://api-docs.deepseek.com/guides/thinking_mode
- DeepSeek JSON Output: https://api-docs.deepseek.com/guides/json_mode
- xAI Reasoning docs: https://docs.x.ai/developers/model-capabilities/text/reasoning

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM backend を、xAI Grok と DeepSeek のどちらでも使えるようにする。
- 既定値は既存互換の xAI / `grok-4-1-fast` のまま維持する。
- DeepSeek を選んだ場合は、`deepseek-v4-flash` を thinking mode enabled / `reasoning_effort="max"` で呼び出せるようにする。
- 切り替えは Discord command ではなく、起動時の環境変数で行う。

今回必ず対応すること:
1. 既存の `XAI_API_KEY` / `XAI_MODEL=grok-4-1-fast` による xAI 呼び出しを維持すること。
2. 新しく `LLM_PROVIDER=xai|deepseek` を追加し、`xai` を既定値にすること。
3. `LLM_PROVIDER=deepseek` の場合、`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` / `DEEPSEEK_THINKING` / `DEEPSEEK_REASONING_EFFORT` を読むこと。
4. DeepSeek V4 Flash は OpenAI ChatCompletions 互換 endpoint で呼び出すこと。
5. DeepSeek thinking mode は `extra_body={"thinking": {"type": "enabled"}}` と `reasoning_effort="max"` で明示すること。
6. xAI の `grok-4-1-fast` には `reasoning_effort` や `reasoning` を送らないこと。
7. 既存の `LLMAction` / `LLMActionDecider` / `LLMAdapter` / fire-and-forget 設計を壊さないこと。

最重要ルール:
- まず既存実装を読み、現在の LLM 呼び出し、設定、テスト構成を把握してから修正すること。
- `domain/` は純粋ロジックのまま保つ。Discord I/O、SQLite I/O、LLM API I/O を domain に入れないこと。
- LLM provider 切り替えは `services/llm_service.py`, `config.py`, `main.py` 周辺に閉じ込めること。
- DB schema は変更しないこと。
- slash command は追加しないこと。
- ゲームルール、状態遷移、prompt builder の人狼戦略文面、persona、ログ秘匿範囲は変更しないこと。
- xAI / DeepSeek どちらでも、最終的に `LLMAction` Pydantic model で検証してから既存処理へ渡すこと。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `.env.example`
- `README.md`
- `src/wolfbot/config.py`
- `src/wolfbot/main.py`
- `src/wolfbot/services/llm_service.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_service.py`

このリポジトリで確認済みの事実:
- 現在の runtime LLM 呼び出しは `src/wolfbot/services/llm_service.py` にある。
- `XAILLMActionDecider.decide()` は `AsyncOpenAI` を `https://api.x.ai/v1` に向け、`client.chat.completions.create(...)` を呼ぶ。
- 現在は `response_format={"type": "json_schema", "json_schema": RESPONSE_SCHEMA}` を使い、戻り値を `LLMAction.model_validate_json(content)` で検証している。
- `src/wolfbot/main.py` は `make_xai_decider(api_key=settings.XAI_API_KEY.get_secret_value(), model=settings.XAI_MODEL)` で decider を作る。
- `src/wolfbot/config.py` は `XAI_API_KEY: SecretStr` と `XAI_MODEL: str = "grok-4-1-fast"` を必須前提で持っている。
- `.env.example` も xAI 用の環境変数だけを記載している。
- `LLMAdapter` は decider を Protocol として受け取るため、provider 追加は adapter の game logic を変えずに実装できる。

Web 調査で確認済みの外部仕様:
- DeepSeek V4 Preview は 2026-04-24 に公開され、`deepseek-v4-pro` と `deepseek-v4-flash` が API で利用可能。
- DeepSeek の OpenAI format base URL は `https://api.deepseek.com`。
- DeepSeek V4 は OpenAI ChatCompletions と Anthropic API の両方をサポートするが、この bot では既存依存の `openai` SDK と ChatCompletions を使う。
- DeepSeek thinking mode は既定で enabled だが、実装では明示的に `extra_body={"thinking": {"type": "enabled"}}` を送る。
- DeepSeek thinking effort は OpenAI format では `reasoning_effort="high"` または `"max"`。今回は `max` を既定値にする。
- DeepSeek thinking mode では `temperature`, `top_p`, `presence_penalty`, `frequency_penalty` は実質無効なので送らない。
- DeepSeek JSON Output は `response_format={"type": "json_object"}` を使い、prompt 側に `json` という語と期待する JSON 形式を含める必要がある。
- xAI 公式 docs では、`grok-4-1-fast` は自動 reasoning であり `reasoning_effort` は未対応。送ると error になる。

実装要求

## 1. 環境変数と Settings を provider 切り替え対応にする

必要な仕様:
- `LLM_PROVIDER` を追加する。
- 値は `xai` または `deepseek`。
- 既定値は `xai`。
- `LLM_PROVIDER=xai` の場合は、従来どおり `XAI_API_KEY` が必須。
- `LLM_PROVIDER=deepseek` の場合は、`DEEPSEEK_API_KEY` が必須。
- `XAI_MODEL` の既定値は `grok-4-1-fast` のまま。
- `DEEPSEEK_BASE_URL` の既定値は `https://api.deepseek.com`。
- `DEEPSEEK_MODEL` の既定値は `deepseek-v4-flash`。
- `DEEPSEEK_THINKING` の既定値は `enabled`。
- `DEEPSEEK_REASONING_EFFORT` の既定値は `max`。

実装方針:
- `src/wolfbot/config.py` で `typing.Literal` と Pydantic v2 の validation を使う。
- `XAI_API_KEY` と `DEEPSEEK_API_KEY` は `SecretStr | None` にする。
- provider に応じて必要な key がない場合だけ startup validation error にする。
- 既存の `.env` で `XAI_API_KEY` が設定されている利用者は、`LLM_PROVIDER` 未指定でもこれまでどおり起動できるようにする。

`.env.example` は以下の形に更新すること:

```env
DISCORD_TOKEN=
LLM_PROVIDER=xai

XAI_API_KEY=
XAI_MODEL=grok-4-1-fast

DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_THINKING=enabled
DEEPSEEK_REASONING_EFFORT=max

DISCORD_GUILD_ID=
MAIN_TEXT_CHANNEL_ID=
MAIN_VOICE_CHANNEL_ID=
WOLFBOT_DB_PATH=./wolfbot.db
LOG_LEVEL=INFO
```

## 2. provider neutral な decider factory を追加する

必要な仕様:
- `main.py` は provider の if 分岐を直接大きく持たない。
- `services/llm_service.py` 側に provider-aware factory を作る。
- 推奨名は `make_llm_decider(settings: Settings, timeout: float = 30.0) -> LLMActionDecider`。
- 既存の `make_xai_decider(...)` は残してよいが、`main.py` は新 factory を使う。
- `XAILLMActionDecider` は既存互換を維持する。
- DeepSeek 用に `DeepSeekLLMActionDecider` を追加する。

実装方針:
- `Settings` を `TYPE_CHECKING` で参照するか、循環 import にならない形にする。
- `AsyncOpenAI` は引き続き lazy import する。
- xAI client:
  - `AsyncOpenAI(api_key=xai_api_key, base_url="https://api.x.ai/v1")`
- DeepSeek client:
  - `AsyncOpenAI(api_key=deepseek_api_key, base_url=deepseek_base_url)`
- どちらの decider も `LLMActionDecider` Protocol の `decide(system_prompt, user_context)` を満たす。

## 3. xAI 呼び出しでは reasoning effort を送らない

必要な仕様:
- `grok-4-1-fast` には `reasoning_effort` を送らない。
- `reasoning` / `extra_body={"thinking": ...}` も送らない。
- 既存どおり JSON schema structured output を使う。

xAI の request shape:

```python
resp = await self.client.chat.completions.create(
    model=self.model,
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_context},
    ],
    response_format={
        "type": "json_schema",
        "json_schema": RESPONSE_SCHEMA,
    },
    timeout=self.timeout,
)
```

やってはいけないこと:
- xAI 側に `reasoning_effort` を渡す。
- xAI 側に DeepSeek の `extra_body={"thinking": ...}` を渡す。
- Grok 用 model 名を DeepSeek 用設定に混ぜる。

## 4. DeepSeek V4 Flash は thinking max effort で呼ぶ

必要な仕様:
- `LLM_PROVIDER=deepseek` の場合、`DEEPSEEK_MODEL=deepseek-v4-flash` を使う。
- `DEEPSEEK_THINKING=enabled` なら `extra_body={"thinking": {"type": "enabled"}}` を送る。
- `DEEPSEEK_REASONING_EFFORT=max` を `reasoning_effort="max"` として送る。
- `DEEPSEEK_THINKING=disabled` も許容し、disabled の場合は `extra_body={"thinking": {"type": "disabled"}}` を送る。
- `DEEPSEEK_THINKING=disabled` の場合は `reasoning_effort` を送らない。
- thinking enabled の場合、`reasoning_effort` は `high` または `max` だけを許す。

DeepSeek の request shape:

```python
kwargs: dict[str, object] = {
    "model": self.model,
    "messages": [
        {"role": "system", "content": deepseek_system_prompt},
        {"role": "user", "content": user_context},
    ],
    "response_format": {"type": "json_object"},
    "timeout": self.timeout,
    "extra_body": {"thinking": {"type": self.thinking}},
}
if self.thinking == "enabled":
    kwargs["reasoning_effort"] = self.reasoning_effort

resp = await self.client.chat.completions.create(**kwargs)  # type: ignore[arg-type]
```

注意:
- `openai` SDK の型定義が provider-specific 追加引数に追いつかない可能性がある。既存の xAI 呼び出しと同様、最小限の `type: ignore` は許容する。
- `temperature`, `top_p`, `presence_penalty`, `frequency_penalty` は送らない。
- DeepSeek は final answer を `message.content` に返す。`reasoning_content` は使わず、ログにも保存しない。
- thinking の chain-of-thought を Discord や DB に出してはいけない。

## 5. DeepSeek 用 JSON Output prompt を明示する

必要な仕様:
- DeepSeek JSON Output は `json_object` なので、xAI の `json_schema` と同じ制約を API だけでは保証できない。
- そのため DeepSeek 呼び出し時だけ system prompt の末尾に、JSON 出力契約を追加する。
- 最終的な検証は必ず `LLMAction.model_validate_json(content)` で行う。
- validation に失敗した場合は既存の tenacity retry 対象にし、最終失敗時は既存どおり caller 側で skip fallback される。

DeepSeek 追加 prompt に必ず含める内容:
- `json` という語。
- 「最終出力は JSON object だけ。Markdown code fence や説明文を出さない」。
- 必須 fields:
  - `intent`
  - `public_message`
  - `target_name`
  - `reason_summary`
  - `confidence`
- `intent` enum:
  - `speak`
  - `vote`
  - `night_action`
  - `skip`
- `target_name` は string または null。
- `confidence` は 0 以上 1 以下の number。
- 例 JSON。

例:

```text
## JSON output contract for this API call
Return only one valid json object. Do not wrap it in markdown. Do not include explanations.
Required shape:
{
  "intent": "speak|vote|night_action|skip",
  "public_message": "",
  "target_name": null,
  "reason_summary": "",
  "confidence": 0.5
}
```

実装方針:
- `DeepSeekLLMActionDecider` 内に private helper を置いてよい。
- 既存の `src/wolfbot/prompts/llm_system_prompt.md` は変更しないこと。DeepSeek API compatibility のための出力契約は decider 側で追記する。
- xAI 側にはこの追加 prompt を入れないこと。xAI は `json_schema` を使えるため既存挙動を維持する。

## 6. README / CLAUDE / IMPLEMENTATION_PROMPT の扱い

必要な仕様:
- `.env.example` は必ず更新する。
- `README.md` は環境変数一覧だけ最小更新する。
- `CLAUDE.md` は LLM integration section と環境変数一覧だけ最小更新する。
- `prompts/IMPLEMENTATION_PROMPT.md` は初期仕様書なので、今回の実装に合わせて LLM API 方針だけ最小更新する。

文面で固定すること:
- 既定 provider は xAI。
- xAI `grok-4-1-fast` には reasoning effort を送らない。
- DeepSeek `deepseek-v4-flash` は thinking enabled / max effort が既定。
- DeepSeek は JSON schema ではなく JSON Output + Pydantic validation で扱う。
- provider 切り替えは `LLM_PROVIDER` と各 provider の env vars で行う。

## 7. テストを追加・更新する

必要なテスト:

### `tests/test_llm_structured_output.py`
- 既存の `LLMAction` schema tests は維持する。
- DeepSeek 用 JSON contract helper が `json` と必須 fields を含むことを確認する。
- DeepSeek の返却 JSON が `LLMAction` に parse されることを確認する。

### `tests/test_llm_service.py`
- fake OpenAI client を使い、xAI decider が `reasoning_effort` と `extra_body` を送らないことを検証する。
- fake OpenAI client を使い、DeepSeek decider が `response_format={"type": "json_object"}` を送ることを検証する。
- fake OpenAI client を使い、DeepSeek thinking enabled では `reasoning_effort="max"` と `extra_body={"thinking": {"type": "enabled"}}` を送ることを検証する。
- DeepSeek thinking disabled では `reasoning_effort` を送らず、`extra_body={"thinking": {"type": "disabled"}}` を送ることを検証する。
- xAI と DeepSeek の両方で、返却 `message.content` を `LLMAction.model_validate_json(...)` することを検証する。

### `tests/test_config.py` を新規追加してよい
- `LLM_PROVIDER` 未指定時に `xai` になること。
- `LLM_PROVIDER=xai` で `XAI_API_KEY` がない場合は validation error。
- `LLM_PROVIDER=deepseek` で `DEEPSEEK_API_KEY` がない場合は validation error。
- `LLM_PROVIDER=deepseek` では `XAI_API_KEY` がなくても validation error にならないこと。
- `DEEPSEEK_REASONING_EFFORT` は `high|max` だけを許すこと。
- `DEEPSEEK_THINKING` は `enabled|disabled` だけを許すこと。

既存テスト群は壊さないこと:
- `tests/test_llm_structured_output.py`
- `tests/test_llm_service.py`
- `tests/test_llm_resolver.py`
- `tests/test_llm_trigger.py`
- `tests/test_llm_prompt_builder.py`

## 8. やってはいけないこと

- Discord command で provider を切り替える機能を追加する。
- provider 設定を SQLite に永続化する。
- player / game / seat schema を変える。
- LLM の public/private log スコープを変える。
- DeepSeek の `reasoning_content` を DB や Discord に出す。
- xAI に `reasoning_effort` を送る。
- DeepSeek thinking enabled 時に sampling parameters を追加する。
- LLM prompt の人狼戦略、人格、フェイズ進行、候補 token 仕様を無関係に変更する。
- `domain/` に provider-specific な分岐を入れる。

受け入れ条件:
- `.env` 未変更の既存 xAI 利用者は、`LLM_PROVIDER` 未指定でもこれまでどおり `grok-4-1-fast` を使える。
- `LLM_PROVIDER=deepseek` と `DEEPSEEK_API_KEY` を設定すると、`deepseek-v4-flash` が使われる。
- DeepSeek 呼び出しでは thinking mode が enabled、reasoning effort が max になる。
- Grok 呼び出しでは `reasoning_effort` を送らない。
- xAI / DeepSeek どちらでも `LLMAction` の validation を通った JSON だけが既存 `LLMAdapter` に渡る。
- 既存の fire-and-forget、stale check、retry、fallback、target resolver が壊れない。
- README / CLAUDE / `.env.example` が新しい設定と矛盾しない。

実行する検証コマンド:
- `uv run pytest tests/test_config.py tests/test_llm_structured_output.py tests/test_llm_service.py tests/test_llm_resolver.py tests/test_llm_trigger.py`
- `uv run pytest tests`
- `uv run ruff check src tests`
- `uv run mypy`

最後に簡潔に報告すること:
- 何を変えたか
- どのファイルを変えたか
- 実行した検証コマンドと結果
- 残課題があればその内容
```
