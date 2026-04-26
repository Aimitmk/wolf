# `wolfbot` 2026-04-26 Gemini Vertex AI ADC Migration Prompt

この文書は、このリポジトリの `wolfbot` を「Gemini を使う場合は AI Studio / Gemini Developer API ではなく、Vertex AI を ADC/IAM 認証で使う」方針へ更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、既存の xAI Grok / DeepSeek 切り替えを壊さずに、`LLM_PROVIDER=gemini` の実体だけを Google AI Studio API key 経由から Vertex AI ADC/IAM 経由へ移行することです。Vertex AI Express mode や API key 認証は対応しません。

調査済みの一次情報:
- Google Gen AI Python SDK: https://googleapis.github.io/python-genai/
- Vertex AI Gen AI SDK overview: https://cloud.google.com/vertex-ai/generative-ai/docs/sdks/overview
- Migrate from Google AI Studio to Vertex AI: https://cloud.google.com/vertex-ai/generative-ai/docs/migrate/migrate-google-ai
- Gemini 3 Flash on Vertex AI: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/3-flash
- Vertex AI SDK migration guide: https://cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk
- Application Default Credentials: https://cloud.google.com/docs/authentication/application-default-credentials

## Prompt

````md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` で、`LLM_PROVIDER=gemini` の場合は Google AI Studio / Gemini Developer API ではなく、Vertex AI の Gemini API を使う。
- Gemini の認証は ADC/IAM のみにする。API key 認証と Vertex AI Express mode は実装しない。
- 既定 provider は既存互換の `LLM_PROVIDER=xai` のまま維持する。
- 既存の xAI Grok / DeepSeek の request shape、設定、テスト、挙動を壊さない。
- 既存の `LLMAction` / `LLMActionDecider` / `LLMAdapter` / fire-and-forget 設計を壊さない。

今回必ず対応すること:
1. `LLM_PROVIDER=gemini` の Gemini client を `genai.Client(api_key=...)` から `genai.Client(vertexai=True, project=..., location=..., http_options=...)` に変更すること。
2. `GEMINI_API_KEY` は Gemini provider の必須条件から外し、docs / `.env.example` でも Gemini 用 API key として案内しないこと。
3. `GEMINI_VERTEX_PROJECT` を追加し、`LLM_PROVIDER=gemini` のとき必須にすること。
4. `GEMINI_VERTEX_LOCATION` を追加し、既定値を `global` にすること。
5. `GEMINI_MODEL` の既定値は `gemini-3-flash-preview` のままにすること。
6. `GEMINI_THINKING_LEVEL` の既定値は `low` のままにすること。
7. Gemini の structured output は引き続き `response_mime_type="application/json"` と `response_json_schema=RESPONSE_SCHEMA["schema"]` を使うこと。
8. Gemini の最終応答は、引き続き `LLMAction.model_validate_json(...)` で検証してから既存処理へ渡すこと。
9. Gemini の内部思考や thought signature を Discord、SQLite、ログに保存・表示しないこと。
10. `main.py` は provider 分岐を直接大きく持たず、既存の `make_llm_decider(settings)` で decider を作り続けること。

禁止事項:
- `google-generativeai` を追加しないこと。
- deprecated な `vertexai.generative_models` / `vertexai.language_models` / `vertexai.vision_models` を使わないこと。
- Vertex AI Express mode を実装しないこと。
- `genai.Client(api_key=...)` を Gemini provider の runtime path で使わないこと。
- `GEMINI_API_KEY` だけで `LLM_PROVIDER=gemini` が起動できる状態を残さないこと。
- DB schema を変更しないこと。
- slash command を追加しないこと。
- ゲームルール、状態遷移、prompt builder の人狼戦略文面、persona、ログ秘匿範囲を変更しないこと。
- Gemini 移行のために `src/wolfbot/prompts/llm_system_prompt.md` や `src/wolfbot/llm/prompt_builder.py` のゲーム戦術文面を変更しないこと。

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
- `tests/test_llm_service.py`
- 必要に応じて `tests/test_llm_structured_output.py`

このリポジトリで確認済みの現在状態:
- runtime LLM 呼び出しは `src/wolfbot/services/llm_service.py` にある。
- `LLMActionDecider` は `decide(system_prompt: str, user_context: str) -> LLMAction` の Protocol。
- `LLMAdapter` は provider ではなく Protocol に依存しているため、Vertex AI 移行は adapter のゲーム進行ロジックを変えずに実装できる。
- `make_llm_decider(settings)` が `settings.LLM_PROVIDER` に応じて xAI / DeepSeek / Gemini decider を返す。
- 既存の Gemini 実装は公式 `google-genai` SDK を使っているが、client construction が `api_key=...` 前提で、AI Studio / Gemini Developer API 側の認証になっている。
- `src/wolfbot/config.py` は現在 `GEMINI_API_KEY` を `LLM_PROVIDER=gemini` の必須値として validation している。
- `.env.example`, `README.md`, `CLAUDE.md` には Gemini API key / Google Gemini API key の説明が残っている。
- `pyproject.toml` にはすでに `google-genai` 依存がある。これを維持し、`google-cloud-aiplatform` は Gemini 呼び出し用には追加しない。

Web 調査で確認済みの外部仕様:
- Google Gen AI Python SDK は Gemini Developer API と Vertex AI API の両方をサポートする。
- Vertex AI API を使う Python client は `genai.Client(vertexai=True, project="...", location="...")` で作成できる。
- Google Gen AI SDK の Vertex AI 利用では、環境変数 `GOOGLE_GENAI_USE_VERTEXAI=true`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` でも client を構成できるが、この bot では project/location を `Settings` から明示的に渡す。
- ADC は `GOOGLE_APPLICATION_CREDENTIALS`、`gcloud auth application-default login` で作成された local ADC、実行環境に attach された service account などから credentials を探す。
- Gemini 3 Flash Preview の Vertex AI model ID は `gemini-3-flash-preview`。
- Gemini 3 Flash は system instructions, structured output, thinking をサポートする。
- Gemini 3 Flash の thinking level は `minimal`, `low`, `medium`, `high` を指定できる。
- Gemini 3 では `thinking_level` が推奨され、`thinking_budget` と同じ request に混在させない。
- Gemini 3 Flash on Vertex AI の supported region は `global`。
- Vertex AI SDK の generative modules は deprecation path にあり、Gemini 呼び出しは Google Gen AI SDK を使う。

実装要求

## 1. Settings を Vertex AI ADC 専用にする

必要な仕様:
- `LLM_PROVIDER` は引き続き `xai`, `deepseek`, `gemini` のいずれか。
- 既定値は `xai`。
- `LLM_PROVIDER=xai` の場合は、従来どおり `XAI_API_KEY` が必須。
- `LLM_PROVIDER=deepseek` の場合は、従来どおり `DEEPSEEK_API_KEY` が必須。
- `LLM_PROVIDER=gemini` の場合は、`GEMINI_VERTEX_PROJECT` が必須。
- `LLM_PROVIDER=gemini` の場合でも、`GEMINI_API_KEY` は不要。
- `GEMINI_VERTEX_LOCATION` の既定値は `global`。
- `GEMINI_MODEL` の既定値は `gemini-3-flash-preview`。
- `GEMINI_THINKING_LEVEL` の既定値は `high`。
- `GEMINI_THINKING_LEVEL` は `minimal`, `low`, `medium`, `high` のみ許可する。

実装方針:
- `src/wolfbot/config.py` で `GEMINI_VERTEX_PROJECT: str | None = None` を追加する。
- `src/wolfbot/config.py` で `GEMINI_VERTEX_LOCATION: str = "global"` を追加する。
- `model_validator(mode="after")` は `LLM_PROVIDER=gemini` のとき `GEMINI_VERTEX_PROJECT` が空または未設定なら validation error にする。
- `GEMINI_API_KEY` は削除してよい。後方互換のために field として一時的に残す場合でも、validation と factory wiring では一切使わないこと。
- 既存 `.env` に古い `GEMINI_API_KEY` が残っていても、`SettingsConfigDict(extra="ignore")` によって不要値として無視される設計でよい。

期待する `.env.example` の Gemini 部分:

```env
# Google Gemini on Vertex AI (required when LLM_PROVIDER=gemini)
# Authenticate with Application Default Credentials:
#   local: gcloud auth application-default login
#   production: use an attached service account with Vertex AI permissions
GEMINI_VERTEX_PROJECT=
GEMINI_VERTEX_LOCATION=global
GEMINI_MODEL=gemini-3-flash-preview
GEMINI_THINKING_LEVEL=low
```

`.env.example` からは Gemini 用の `GEMINI_API_KEY=` を削除すること。

## 2. Gemini client を Vertex AI client に変更する

必要な仕様:
- `GeminiLLMActionDecider` の `decide(...)` は、可能な限り既存の request shape を維持する。
- `make_gemini_decider(...)` の引数は API key ではなく、`project`, `location`, `model`, `thinking_level`, `timeout` にする。
- `timeout` は既存 decider と同じ public interface のため残す。
- Google Gen AI SDK では request timeout を `types.HttpOptions(timeout=...)` に載せる。OpenAI SDK のように `generate_content(...)` へ `timeout=` を直接渡さない。
- Vertex AI client は `genai.Client(vertexai=True, project=project, location=location, http_options=...)` で作る。
- ADC credentials は Google auth library に任せるため、service account key の読み込みや `google.auth` の手動処理を bot 側で実装しない。

推奨実装イメージ:

```python
def make_gemini_decider(
    project: str,
    location: str,
    model: str,
    thinking_level: Literal["minimal", "low", "medium", "high"] = "low",
    timeout: float = 30.0,
) -> GeminiLLMActionDecider:
    """Build a Vertex AI Gemini-backed decider. Imports google-genai lazily."""
    from google import genai
    from google.genai import types

    client = genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=types.HttpOptions(timeout=int(timeout * 1000)),
    )
    return GeminiLLMActionDecider(
        client=client,
        model=model,
        thinking_level=thinking_level,
        timeout=timeout,
    )
```

`make_llm_decider(settings, timeout=30.0)` の Gemini branch は次の形にする:

```python
if settings.LLM_PROVIDER == "gemini":
    assert settings.GEMINI_VERTEX_PROJECT is not None
    return make_gemini_decider(
        project=settings.GEMINI_VERTEX_PROJECT,
        location=settings.GEMINI_VERTEX_LOCATION,
        model=settings.GEMINI_MODEL,
        thinking_level=settings.GEMINI_THINKING_LEVEL,
        timeout=timeout,
    )
```

`GeminiLLMActionDecider.decide(...)` は次の挙動を維持する:
- `await self.client.aio.models.generate_content(...)` を使う。
- `model=self.model`
- `contents=user_context`
- `config=types.GenerateContentConfig(...)`
- `system_instruction=system_prompt`
- `response_mime_type="application/json"`
- `response_json_schema=RESPONSE_SCHEMA["schema"]`
- `thinking_config=types.ThinkingConfig(thinking_level=self.thinking_level)`
- `resp.text or "{}"` を `LLMAction.model_validate_json(...)` に渡す。

## 3. docs を AI Studio から Vertex AI ADC へ更新する

必要な仕様:
- `README.md`, `CLAUDE.md`, `.env.example` から「Gemini は `GEMINI_API_KEY` を設定する」という案内を削除する。
- Gemini provider の説明は「Vertex AI の Gemini API を Google Gen AI SDK で呼び、ADC/IAM で認証する」に統一する。
- local development では `gcloud auth application-default login` を案内する。
- production では実行環境の service account に Vertex AI を呼べる IAM 権限を付与する方針を案内する。
- `GOOGLE_APPLICATION_CREDENTIALS` は ADC の選択肢として説明してよいが、service account key は漏洩リスクがあるため第一推奨にはしない。
- Vertex AI Express mode / API key mode はこの bot では非対応と明記する。

README の環境変数表は次を反映する:
- `GEMINI_VERTEX_PROJECT`: `LLM_PROVIDER=gemini` のとき必須。Vertex AI を使う Google Cloud project ID。
- `GEMINI_VERTEX_LOCATION`: 既定値 `global`。Vertex AI Gemini API の location。
- `GEMINI_MODEL`: 既定値 `gemini-3-flash-preview`。
- `GEMINI_THINKING_LEVEL`: 既定値 `low`。`minimal` / `low` / `medium` / `high`。
- `GEMINI_API_KEY` は Gemini provider の設定表から削除する。

CLAUDE の LLM integration 説明は、Gemini endpoint が AI Studio / `generativelanguage.googleapis.com` ではなく Vertex AI / `aiplatform.googleapis.com` 系であることが分かる表現へ更新する。ただし `google-genai` SDK を使うため、コード上は SDK client が endpoint を扱うことも明記する。

## 4. tests を更新する

最低限必要な test:
- `LLM_PROVIDER=gemini` かつ `GEMINI_VERTEX_PROJECT` 未設定なら `Settings` validation error になる。
- `LLM_PROVIDER=gemini` かつ `GEMINI_VERTEX_PROJECT` 設定済みなら `XAI_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY` なしで construct できる。
- `GEMINI_VERTEX_LOCATION` の既定値が `global` である。
- `GEMINI_MODEL` の既定値が `gemini-3-flash-preview` である。
- `GEMINI_THINKING_LEVEL` の既定値が `low` である。
- `GEMINI_THINKING_LEVEL` に未知値を渡すと validation error になる。
- `GEMINI_API_KEY` だけを渡して `LLM_PROVIDER=gemini` にしても、`GEMINI_VERTEX_PROJECT` がなければ validation error になる。
- `make_llm_decider(settings)` が `LLM_PROVIDER=gemini` で `GeminiLLMActionDecider` を返す。
- Gemini decider の `decide(...)` は引き続き `response_json_schema`, `thinking_level`, `system_instruction`, `contents` を送る。

`make_gemini_decider(...)` の client construction をテストする場合は、`google.genai.Client` を monkeypatch し、次を確認する:
- `vertexai is True`
- `project == settings.GEMINI_VERTEX_PROJECT`
- `location == settings.GEMINI_VERTEX_LOCATION`
- `api_key` が渡されていない
- `http_options` が渡される

既存の xAI / DeepSeek tests は壊さないこと。既存の xAI / DeepSeek request shape assertions が落ちる変更はしない。

## 5. acceptance criteria

実装完了条件:
- `LLM_PROVIDER=xai` は従来どおり `XAI_API_KEY` で動く。
- `LLM_PROVIDER=deepseek` は従来どおり `DEEPSEEK_API_KEY` で動く。
- `LLM_PROVIDER=gemini` は `GEMINI_VERTEX_PROJECT` と ADC/IAM credentials で Vertex AI Gemini API を呼ぶ。
- `LLM_PROVIDER=gemini` は `GEMINI_API_KEY` を要求しない。
- `LLM_PROVIDER=gemini` は `genai.Client(api_key=...)` を使わない。
- Gemini の出力 validation、retry、structured output、thinking level、内部思考を読まない方針は維持されている。
- README / CLAUDE / `.env.example` の案内が Vertex AI ADC 専用になっている。

最後に必ず実行するコマンド:

```bash
uv run pytest tests/test_config.py tests/test_llm_service.py
uv run pytest tests
uv run ruff check src tests
uv run mypy
```

もし環境や sandbox の都合で実行できないコマンドがあれば、何が実行できなかったか、どこまで確認できたかを明記すること。
````
