# `wolfbot` 2026-04-26 LLM Provider Switching / Gemini 3 Flash Update Prompt

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、既存の xAI Grok / DeepSeek 切り替えを壊さずに、Google Gemini 3 Flash も LLM backend として選べるようにし、必要に応じて Grok / DeepSeek / Gemini を起動時の環境変数で切り替えられるようにすることです。

調査済みの一次情報:
- Gemini 3 Developer Guide: https://ai.google.dev/gemini-api/docs/gemini-3
- Gemini model list: https://ai.google.dev/gemini-api/docs/models/gemini
- Gemini structured output: https://ai.google.dev/gemini-api/docs/structured-output
- Gemini thinking guide: https://ai.google.dev/gemini-api/docs/thinking
- Gemini API libraries: https://ai.google.dev/gemini-api/docs/downloads
- Google Gen AI Python SDK: https://googleapis.github.io/python-genai/

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の LLM backend を、xAI Grok / DeepSeek / Google Gemini のいずれでも使えるようにする。
- 既定値は既存互換の `LLM_PROVIDER=xai` のまま維持する。
- `LLM_PROVIDER=gemini` を選んだ場合は、Gemini 3 Flash Preview (`gemini-3-flash-preview`) を使えるようにする。
- 切り替えは Discord command ではなく、起動時の環境変数で行う。
- 既存の `LLMAction` / `LLMActionDecider` / `LLMAdapter` / fire-and-forget 設計を壊さない。

今回必ず対応すること:
1. 既存の xAI Grok と DeepSeek の動作、request shape、既定値、テストを維持すること。
2. `LLM_PROVIDER` を `xai|deepseek|gemini` に拡張し、既定値は `xai` のままにすること。
3. `LLM_PROVIDER=gemini` の場合、`GEMINI_API_KEY` / `GEMINI_MODEL` / `GEMINI_THINKING_LEVEL` を読むこと。
4. Gemini 3 Flash は公式 Google Gen AI Python SDK (`google-genai`) で呼び出すこと。deprecated な `google-generativeai` は使わないこと。
5. Gemini の最終応答も、xAI / DeepSeek と同じく `LLMAction.model_validate_json(...)` で検証してから既存処理へ渡すこと。
6. Gemini の内部思考や thought signature を Discord、SQLite、ログに保存・表示しないこと。
7. `main.py` は provider 分岐を直接大きく持たず、既存の `make_llm_decider(settings)` で decider を作り続けること。

最重要ルール:
- まず既存実装を読み、現在の LLM 呼び出し、設定、テスト構成を把握してから修正すること。
- `domain/` は純粋ロジックのまま保つ。Discord I/O、SQLite I/O、LLM API I/O を domain に入れないこと。
- LLM provider 切り替えは `config.py`, `services/llm_service.py`, `main.py`, docs/tests 周辺に閉じ込めること。
- DB schema は変更しないこと。
- slash command は追加しないこと。
- ゲームルール、状態遷移、prompt builder の人狼戦略文面、persona、ログ秘匿範囲は変更しないこと。
- Gemini 対応のために `src/wolfbot/prompts/llm_system_prompt.md` や `src/wolfbot/llm/prompt_builder.py` のゲーム戦術文面を変更しないこと。
- 実装後は必ずテスト・lint・型チェックを走らせ、結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `.env.example`
- `README.md`
- `pyproject.toml`
- `src/wolfbot/config.py`
- `src/wolfbot/main.py`
- `src/wolfbot/services/llm_service.py`
- `tests/test_config.py`
- `tests/test_llm_structured_output.py`
- `tests/test_llm_service.py`

このリポジトリで確認済みの事実:
- runtime LLM 呼び出しは `src/wolfbot/services/llm_service.py` にある。
- `LLMActionDecider` は `decide(system_prompt: str, user_context: str) -> LLMAction` の Protocol。
- `LLMAdapter` は provider ではなく Protocol に依存しているため、Gemini 対応は adapter のゲーム進行ロジックを変えずに実装できる。
- `XAILLMActionDecider` は OpenAI-compatible chat completions で xAI を呼び、`response_format={"type":"json_schema", "json_schema": RESPONSE_SCHEMA}` を使う。
- `DeepSeekLLMActionDecider` は OpenAI-compatible chat completions で DeepSeek を呼び、`response_format={"type":"json_object"}` と system prompt 末尾の JSON contract を使う。
- `make_llm_decider(settings)` は現在 `settings.LLM_PROVIDER` に応じて xAI / DeepSeek decider を返す。
- `src/wolfbot/config.py` は `LLM_PROVIDER: Literal["xai", "deepseek"]` と provider key validator を持つ。
- `.env.example`, `README.md`, `CLAUDE.md` は現在 xAI / DeepSeek 中心の説明になっている。
- `pyproject.toml` には `google-genai` 依存がまだない。

Web 調査で確認済みの外部仕様:
- Gemini 3 Flash Preview の model ID は `gemini-3-flash-preview`。
- Gemini 3 Flash は structured outputs と thinking をサポートする。
- Gemini 3 Flash の thinking level は `minimal`, `low`, `medium`, `high` を指定できる。
- Gemini 3 の thinking level 未指定時は既定で `high` だが、この bot では Discord の進行遅延を避けるため `low` を既定値にする。
- Gemini 3 では従来の `thinking_budget` より `thinking_level` が推奨される。`thinking_level` と `thinking_budget` を同じ request に混在させないこと。
- Google は Gemini API 用の公式 SDK として `google-genai` を推奨しており、legacy libraries からの移行を推奨している。
- Google Gen AI Python SDK の async 呼び出しは `await client.aio.models.generate_content(...)`。
- Gemini structured output は `response_mime_type="application/json"` と `response_json_schema=...` で JSON Schema を渡せる。

実装要求

## 1. 依存関係を追加する

必要な仕様:
- `pyproject.toml` の runtime dependencies に `google-genai` を追加する。
- deprecated な `google-generativeai` は追加しない。
- 既存の `openai` 依存は xAI / DeepSeek 用に残す。

実装方針:
- 既存の依存管理方針に合わせ、`uv sync` で lock が更新される構成にする。
- バージョン下限は、Gemini 3 Flash / `thinking_level` / async `client.aio.models.generate_content` / `response_json_schema` をサポートするものにする。迷った場合は調査時点で最新系の `google-genai>=1.69` を使う。

## 2. 環境変数と Settings を Gemini 対応にする

必要な仕様:
- `LLM_PROVIDER` の値を `xai`, `deepseek`, `gemini` にする。
- 既定値は `xai`。
- `LLM_PROVIDER=xai` の場合は、従来どおり `XAI_API_KEY` が必須。
- `LLM_PROVIDER=deepseek` の場合は、従来どおり `DEEPSEEK_API_KEY` が必須。
- `LLM_PROVIDER=gemini` の場合は、`GEMINI_API_KEY` が必須。
- `LLM_PROVIDER=gemini` の場合、`XAI_API_KEY` と `DEEPSEEK_API_KEY` は不要。
- `GEMINI_MODEL` の既定値は `gemini-3-flash-preview`。
- `GEMINI_THINKING_LEVEL` の既定値は `low`。
- `GEMINI_THINKING_LEVEL` は `minimal`, `low`, `medium`, `high` のみ許可する。

実装方針:
- `src/wolfbot/config.py` で `typing.Literal` と Pydantic v2 の validation を使う。
- `GEMINI_API_KEY` は `SecretStr | None` にする。
- provider に応じて必要な key がない場合だけ startup validation error にする。
- 既存の `.env` で `XAI_API_KEY` が設定されている利用者は、`LLM_PROVIDER` 未指定でもこれまでどおり起動できるようにする。

`.env.example` は provider 3 種が分かるように更新すること:

```env
DISCORD_TOKEN=
DISCORD_GUILD_ID=
MAIN_TEXT_CHANNEL_ID=
MAIN_VOICE_CHANNEL_ID=
WOLFBOT_DB_PATH=./wolfbot.db
LOG_LEVEL=INFO

# LLM provider: "xai" (default), "deepseek", or "gemini". Lowercase only.
LLM_PROVIDER=xai

# xAI (required when LLM_PROVIDER=xai)
XAI_API_KEY=
XAI_MODEL=grok-4-1-fast-reasoning

# DeepSeek (required when LLM_PROVIDER=deepseek)
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_THINKING=enabled
DEEPSEEK_REASONING_EFFORT=max

# Google Gemini (required when LLM_PROVIDER=gemini)
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3-flash-preview
GEMINI_THINKING_LEVEL=low
```

## 3. Gemini decider を追加する

必要な仕様:
- `services/llm_service.py` に `GeminiLLMActionDecider` を追加する。
- `GeminiLLMActionDecider` は `LLMActionDecider` Protocol を満たす。
- constructor は最低限 `client`, `model`, `thinking_level`, `timeout` を受け取れるようにする。
- `timeout` は既存 decider と同じ public interface のため残す。Google Gen AI SDK で request timeout を設定する場合は、SDK の `types.HttpOptions(timeout=...)` に載せる。`generate_content(...)` に OpenAI SDK の `timeout=` をそのまま渡さないこと。
- xAI / DeepSeek の decider 実装は必要がない限り変更しない。

推奨実装:

```python
class GeminiLLMActionDecider:
    """Calls Google Gemini through the official google-genai SDK."""

    def __init__(
        self,
        client: object,
        model: str,
        thinking_level: Literal["minimal", "low", "medium", "high"] = "low",
        timeout: float = 30.0,
    ) -> None:
        self.client = client
        self.model = model
        self.thinking_level = thinking_level
        self.timeout = timeout

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        from google.genai import types

        resp = await self.client.aio.models.generate_content(
            model=self.model,
            contents=user_context,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_json_schema=RESPONSE_SCHEMA["schema"],
                thinking_config=types.ThinkingConfig(
                    thinking_level=self.thinking_level,
                ),
            ),
        )
        content = resp.text or "{}"
        return LLMAction.model_validate_json(content)
```

注意:
- `client` の型は `TYPE_CHECKING` で `google.genai.Client` 相当を参照するか、Protocol / `object` で循環 import と import-time 依存を避けること。
- Gemini の structured output は API 側で JSON Schema を使うが、最終的なアプリ側 validation は必ず `LLMAction.model_validate_json(content)` で行う。
- Gemini 用に DeepSeek の JSON contract suffix を使い回さない。Gemini は `response_json_schema` を使う。
- `temperature`, `top_p`, `presence_penalty`, `frequency_penalty` は今回追加しない。Gemini 3 の公式 guidance では、移行時に明示 temperature を外すことが推奨されている。
- thought signatures や内部 thinking は読まない・保存しない・ログに出さない。

## 4. provider-aware factory を Gemini 対応にする

必要な仕様:
- `services/llm_service.py` に `make_gemini_decider(...)` を追加する。
- `make_llm_decider(settings, timeout=30.0)` は `settings.LLM_PROVIDER == "gemini"` の場合に `GeminiLLMActionDecider` を返す。
- `main.py` は引き続き `make_llm_decider(settings)` だけを呼ぶ。
- `__all__` に `GeminiLLMActionDecider` と `make_gemini_decider` を追加する。

推奨実装:

```python
def make_gemini_decider(
    api_key: str,
    model: str,
    thinking_level: Literal["minimal", "low", "medium", "high"] = "low",
    timeout: float = 30.0,
) -> GeminiLLMActionDecider:
    """Build a Gemini-backed decider. Imports google-genai lazily."""
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=int(timeout * 1000)),
    )
    return GeminiLLMActionDecider(
        client=client,
        model=model,
        thinking_level=thinking_level,
        timeout=timeout,
    )
```

factory branch:

```python
if settings.LLM_PROVIDER == "gemini":
    assert settings.GEMINI_API_KEY is not None
    return make_gemini_decider(
        api_key=settings.GEMINI_API_KEY.get_secret_value(),
        model=settings.GEMINI_MODEL,
        thinking_level=settings.GEMINI_THINKING_LEVEL,
        timeout=timeout,
    )
```

注意:
- Google Gen AI SDK は preview feature 対応のため既定で beta API endpoint を使う。Gemini 3 Flash Preview では明示的に `api_version="v1"` へ固定しない。
- `make_xai_decider(...)` と `make_deepseek_decider(...)` は既存互換のため残す。
- Gemini のために OpenAI compatibility layer を使わない。今回の実装は公式 Google Gen AI SDK に統一する。

## 5. docs を更新する

必要な仕様:
- `README.md` の冒頭、セットアップ、環境変数一覧に Gemini を追加する。
- `CLAUDE.md` の LLM integration / env var 説明に Gemini provider を追加する。
- `pyproject.toml` の project description は必要なら `xAI Grok LLM padding` のような xAI 限定表現を provider-neutral にする。
- `prompts/IMPLEMENTATION_PROMPT.md` は、現行仕様の固定プロンプトとして provider 3 種対応へ更新してよい。ただしゲーム仕様・状態遷移・役職・戦略 prompt の意味は変えない。
- `prompts/README_PROMPT.md` は README 再生成用の古い xAI 限定説明があれば provider 3 種対応へ更新してよい。

README の環境変数一覧には最低限以下を追加する:
- `GEMINI_API_KEY`: `LLM_PROVIDER=gemini` のとき必須。Google Gemini API キー。
- `GEMINI_MODEL`: 既定値 `gemini-3-flash-preview`。使用する Gemini モデル名。
- `GEMINI_THINKING_LEVEL`: 既定値 `low`。Gemini 3 Flash thinking level (`minimal` / `low` / `medium` / `high`)。

## 6. tests を追加・更新する

`tests/test_config.py`:
- 既定 provider が `xai` のままであること。
- `LLM_PROVIDER=gemini` かつ `GEMINI_API_KEY` 未設定なら validation error になること。
- `LLM_PROVIDER=gemini` かつ `GEMINI_API_KEY` 設定済みなら `XAI_API_KEY` / `DEEPSEEK_API_KEY` なしで construct できること。
- `GEMINI_MODEL == "gemini-3-flash-preview"` が既定値であること。
- `GEMINI_THINKING_LEVEL == "low"` が既定値であること。
- `GEMINI_THINKING_LEVEL` に未知値を渡すと validation error になること。

`tests/test_llm_service.py`:
- fake Google Gen AI client を作り、`GeminiLLMActionDecider.decide(...)` が `client.aio.models.generate_content(...)` に以下を渡すことを検証する:
  - `model == "gemini-3-flash-preview"`
  - `contents == user_context`
  - `config.system_instruction == system_prompt`
  - `config.response_mime_type == "application/json"`
  - `config.response_json_schema == RESPONSE_SCHEMA["schema"]`
  - `config.thinking_config.thinking_level == "low"` または設定値
- fake response の `.text` を `LLMAction.model_validate_json(...)` で parse して `LLMAction` を返すこと。
- `make_llm_decider(settings)` が `LLM_PROVIDER=gemini` で `GeminiLLMActionDecider` を返すこと。
- 既存の xAI test で `reasoning_effort` / `extra_body` を送らない保証を維持すること。
- 既存の DeepSeek test で `json_object` / thinking / reasoning_effort の保証を維持すること。

`tests/test_llm_structured_output.py`:
- `RESPONSE_SCHEMA["schema"]` が Gemini structured output に渡せる shape を保つことを確認する。
- 既存の `LLMAction` parse / reject tests は provider-neutral なまま維持する。

## 7. 受け入れ条件

実装後に以下を実行し、結果を報告すること:

```bash
uv run pytest tests/test_config.py tests/test_llm_structured_output.py tests/test_llm_service.py
uv run ruff check src tests
uv run mypy
```

全体の変更確認:
- `LLM_PROVIDER=xai` の既存環境では従来どおり xAI decider が作られる。
- `LLM_PROVIDER=deepseek` の既存環境では従来どおり DeepSeek decider が作られる。
- `LLM_PROVIDER=gemini` かつ `GEMINI_API_KEY` 設定済みなら Gemini decider が作られる。
- `LLM_PROVIDER=gemini` かつ `GEMINI_API_KEY` 未設定なら起動時 validation error になる。
- Gemini の response は JSON structured output と `LLMAction.model_validate_json(...)` の二段で検証される。
- Gemini の内部 thinking / thought signature は DB・Discord・ログに出ない。
- DB schema、slash command、ゲームルール、prompt builder の戦略文面に不要な差分がない。
```
