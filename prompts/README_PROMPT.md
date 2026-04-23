# README 生成プロンプト

この文書は、このリポジトリ向けの `README.md` を生成するための固定プロンプトです。下の `## Prompt` セクションをそのまま別の LLM に渡すことを想定しています。

## Prompt

```md
あなたは、このリポジトリの README.md を作成する技術ライターです。

目的:
- 初めてこの bot を導入する Discord サーバー管理者、ホスト、運用担当者が、README だけでセットアップと基本運用を理解できる状態にする。
- 主読者は開発者ではなく、利用者/サーバー管理者寄りにする。

最重要ルール:
- 出力は README.md の本文となる Markdown のみを返すこと。
- 出力言語は日本語にすること。
- 不明なことは推測で補わず、省略すること。
- 実装されていないものを「ある」と書かないこと。
- Web UI、Docker、CI、Makefile、監視基盤、複数配役対応など、リポジトリに存在しない話題を勝手に追加しないこと。
- 読者が最初に知りたいのは「何の bot か」「何を準備するか」「どう起動するか」「どう使うか」なので、その順で理解しやすく構成すること。

もしリポジトリを読める環境なら、以下を優先的に参照して事実確認してください:
- `CLAUDE.md`
- `pyproject.toml`
- `.env.example`
- `src/wolfbot/config.py`
- `src/wolfbot/main.py`
- `src/wolfbot/services/discord_service.py`
- `prompts/IMPLEMENTATION_PROMPT.md`

ただし、リポジトリを読めない環境でも README が書けるように、以下にこのプロジェクトの既知の事実を列挙します。README はこれらの事実から逸脱しないこと。

# プロジェクトの事実

## 概要
- プロジェクト名は `wolfbot`。
- Discord で 9 人村の人狼を進行する Python 製 bot。
- 人間プレイヤーが 9 人未満のときは、xAI Grok API を使う LLM プレイヤーで不足人数を補完する。
- 人間は VC で会話しながら遊ぶ想定で、LLM がいる場合はメイン text チャンネルの投稿も議論材料として使う。

## 技術スタック
- Python は `>=3.11,<3.12`。
- パッケージ管理は `uv`。
- 主な依存は `discord.py`, `aiosqlite`, `pydantic`, `pydantic-settings`, `python-dotenv`, `openai`, `httpx`, `tenacity`。
- SQLite にゲーム状態を永続化する。

## 起動と開発コマンド
- 依存インストール: `uv sync`
- bot 起動: `uv run wolfbot`
- テスト: `uv run pytest tests`
- 単体テスト例: `uv run pytest tests/test_rules_votes.py`
- Lint: `uv run ruff check src tests`
- Format: `uv run ruff format src tests`
- 型チェック: `uv run mypy`

## 環境変数
- `DISCORD_TOKEN`
- `XAI_API_KEY`
- `XAI_MODEL`。既定値は `grok-4-1-fast`
- `DISCORD_GUILD_ID`
- `MAIN_TEXT_CHANNEL_ID`
- `MAIN_VOICE_CHANNEL_ID`
- `WOLFBOT_DB_PATH`。既定値は `./wolfbot.db`
- `LOG_LEVEL`。既定値は `INFO`

## Discord 側の前提
- slash command は `/wolf` グループで提供される。
- コマンドは設定された 1 つの guild に同期される。
- bot は既存のメイン text チャンネルと既存のメイン VC を使う。
- bot はゲーム作成時に秘密チャンネルとして `wolf-heaven` と `wolf-wolves` という text チャンネルを作成し、ゲーム終了時に削除する。
- bot はチャンネル権限の上書きを更新する。
- bot は `message_content` と `members` intent を使う。
- 運用上、Discord Developer Portal 側で必要な Privileged Gateway Intents を有効にする前提がある。
- 運用上、bot には少なくとも「テキストチャンネル作成/削除」「チャンネル権限変更」「メッセージ送信」「メッセージ閲覧」に必要な権限が要る。
- メイン text チャンネルは、ゲームに関係ないユーザーや観戦者の投稿が混ざらないように、専用チャンネルを使うか、基底権限を事前に管理者が確認する前提である。

## ゲーム仕様
- 9 人村専用。
- 配役は固定で `人狼2 / 狂人1 / 占い師1 / 霊媒師1 / 騎士1 / 村人3`。
- 秘密投票と夜行動は bot DM で行う。
- 死者専用の天国チャンネルがある。
- 人狼専用チャットがある。
- 人間プレイヤーが 9 人未満の場合、Gnosia 風 persona プールから重複なしで LLM を補完する。
- LLM は VC 音声を読まず、メイン text チャンネルの投稿をもとに議論する。
- bot 再起動後は、進行中ゲームの復旧を試みる。
- 締切時に未提出が残っていた場合は `WAITING_HOST_DECISION` に入り、ホストが `/wolf extend` または `/wolf force-skip` を使って再開する。

## 現在の `/wolf` コマンド
- `/wolf create`
- `/wolf join`
- `/wolf leave`
- `/wolf start`
- `/wolf status`
- `/wolf extend`
- `/wolf force-skip`
- `/wolf abort`

## README に必ず含めること
以下の章立てで、読みやすい README を作成してください。見出し名は自然な日本語に調整してよいですが、内容は必ず入れてください。

1. タイトルと概要
- `wolfbot` が何をする bot なのかを 2〜4 文で説明する。
- 「9 人村専用」「人数不足は LLM 補完」「Discord 上で動く」を短く含める。

2. 主な機能
- 箇条書きで主要機能を整理する。
- 例: 固定配役の 9 人村運用、LLM 補完、DM ベースの秘密投票/夜行動、天国チャンネル、人狼専用チャット、SQLite 永続化、再起動復旧。

3. 前提条件
- Python 3.11
- `uv`
- Discord Bot アプリケーション
- xAI API キー
- 既存のメイン text チャンネルとメイン VC

4. セットアップ手順
- 依存インストール
- `.env` の準備
- 必要な Discord 設定
- bot 起動
- 必要なら `.env.example` を参照するよう案内する

5. 環境変数一覧
- 表形式にすると読みやすい
- 変数名、必須/既定値、用途を簡潔にまとめる

6. Discord 側の準備
- 既存のメイン text/VC を用意すること
- Bot に必要な権限と intent を有効にすること
- `MAIN_TEXT_CHANNEL_ID` と `MAIN_VOICE_CHANNEL_ID` を設定すること
- bot が秘密チャンネルを作成/削除すること
- メイン text の基底権限は管理者が事前確認すべきであること
- プレイヤーは bot から DM を受け取れる必要があること

7. 使い方
- 典型的な流れを短い手順で書く
- 例: `/wolf create` -> `/wolf join` -> `/wolf start` -> 進行中は `/wolf status`
- ホスト判断が必要な場合は `/wolf extend` と `/wolf force-skip` を使うこと
- 強制終了は `/wolf abort`

8. コマンド一覧
- `/wolf` コマンドを表または箇条書きで説明する
- 各コマンドの役割を 1 行ずつで十分にまとめる

9. ゲーム進行の要約
- LOBBY から始まり、昼議論、投票、夜行動を繰り返すことを高レベルに説明する
- DM で秘密操作を行うこと
- LLM は必要人数だけ自動補完されること
- 締切超過時はホスト判断待ちになること

10. 運用上の注意
- LLM はメイン text の投稿しか読まないので、LLM を含む村では重要情報を text に流す必要があること
- VC 音声認識や Web UI はないこと
- 9 人村以外の配役には対応していないこと
- メインチャンネル権限と DM 受信設定が実運用上の前提になること

11. 開発者向け補足
- 最後に短く、開発用コマンドだけを載せる
- 詳細なアーキテクチャ解説は省略気味でよい
- `CLAUDE.md` や `prompts/IMPLEMENTATION_PROMPT.md` が詳細仕様の参照先であることを 1〜2 行で触れてよい

## 書き方の指示
- 読者が最初に行動できる README にすること。抽象説明より手順を優先すること。
- 内部実装の責務分割を長々と説明しないこと。
- 章ごとに冗長にしすぎず、実運用に必要な情報を優先すること。
- コマンド、環境変数、チャンネル ID、ファイル名はインラインコードで表記すること。
- セットアップ手順はコピペしやすいコードブロックを使うこと。
- 表を使うなら簡潔にすること。

## README に書いてはいけないこと
- 未実装のコマンドや UI
- 「CI がある」「Docker がある」「本番環境にデプロイ済み」などの断定
- ライセンス、公開 URL、招待リンク、スクリーンショットなど、与えられていない情報
- 監視、SaaS、課金、複数サーバー対応などの推測

最後に、README.md としてそのまま保存できる完成形の Markdown だけを出力してください。解説文、前置き、補足コメントは不要です。
```
