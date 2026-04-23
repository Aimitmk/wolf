# `wolfbot` 起動 import エラー修正プロンプト

この文書は、このリポジトリで `uv run wolfbot` 実行時に発生する import エラーを修正するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- `uv run wolfbot` 実行時に発生する `ModuleNotFoundError: No module named 'wolfbot'` を根本解消する。
- 最小差分で直し、既存のゲーム仕様や Discord 挙動には不要な変更を入れない。

最重要ルール:
- まず根本原因を特定してから直すこと。勘で設定を増やさないこと。
- 修正対象は import / packaging / editable install / `src` layout 周辺に限定すること。
- `domain/`, `services/`, `persistence/`, `llm/`, `ui/` の業務ロジックは、この問題に無関係なら変更しないこと。
- 出力には「何を直したか」「なぜそれで直るか」「どう検証したか」を簡潔に含めること。
- 可能なら既存のコマンド体系 (`uv sync`, `uv run wolfbot`, `uv run pytest tests`, `uv run ruff check src tests`, `uv run mypy`) に乗る形で直すこと。

事前に必ず確認するファイル:
- `pyproject.toml`
- `src/wolfbot/__init__.py`
- `src/wolfbot/main.py`
- `.venv/lib/python3.11/site-packages/_editable_impl_wolfbot.pth`
- 必要なら `.venv/bin/wolfbot`
- 必要なら `CLAUDE.md`

このリポジトリで確認済みの事実:
- 実コードは `src/wolfbot` 配下にある。
- `pyproject.toml` には `[project.scripts] wolfbot = "wolfbot.main:cli"` がある。
- `pyproject.toml` には `[tool.hatch.build.targets.wheel] packages = ["src/wolfbot"]` がある。
- `uv run python -c "import importlib.util; print(importlib.util.find_spec('wolfbot'))"` の結果は `None`。
- `uv run python -c "import sys; print(sys.path)"` では `src` が `sys.path` に入っていない。
- `.venv/lib/python3.11/site-packages/_editable_impl_wolfbot.pth` にはリポジトリの `src` パスが入っている。
- 実際の起動時エラーは以下。

```text
Traceback (most recent call last):
  File "/Users/aimitmk/Documents/2025_Apps/20260423_wolf/.venv/bin/wolfbot", line 4, in <module>
    from wolfbot.main import cli
ModuleNotFoundError: No module named 'wolfbot'
```

やってよいこと:
- `pyproject.toml` の packaging / build / editable install 周辺の修正
- 必要なら再インストール相当の手順 (`uv sync` など)
- 起動 import の検証コマンド実行
- 影響が小さい範囲での補助的な設定修正

やってはいけないこと:
- ゲームルール変更
- slash command 追加や UI 改修
- 無関係なリファクタ
- README や別ドキュメントだけ直して終えること

期待する進め方:
1. `wolfbot` が import できない直接原因を特定する。
2. その原因に対する最小修正を入れる。
3. 必要なら editable install / 仮想環境の再同期を行う。
4. 以下の順で検証する。
   - `uv run python -c "import wolfbot; import wolfbot.main"`
   - `uv run wolfbot`
5. `uv run wolfbot` がその先で `.env` や環境変数不足で止まる場合は、import 問題が解消されたことを明示したうえで別エラーとして報告する。
6. 可能なら追加で `uv run pytest tests`, `uv run ruff check src tests`, `uv run mypy` を実行し、副作用がないことを確認する。

受け入れ条件:
- `wolfbot` パッケージを Python から import できる。
- `uv run wolfbot` 実行時に `ModuleNotFoundError: No module named 'wolfbot'` が再発しない。
- 修正理由が packaging / import 解決の観点で説明できる。
- 変更は最小限で、無関係な振る舞いを変えていない。

最後に、以下を簡潔に報告すること:
- 根本原因
- 変更したファイル
- 実行した検証コマンド
- まだ残っている別系統の問題があればその内容
```
