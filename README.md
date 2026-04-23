# wolfbot

`wolfbot` は、Discord 上で 9 人村の人狼を進行する Python 製 bot です。`/wolf` コマンドでロビー作成から開始、進行確認、延長、強制終了まで操作できます。人間プレイヤーが 9 人に満たない場合は、xAI Grok API を使う LLM プレイヤーが不足人数を補完します。9 人村専用で、人間は主に VC で会話し、LLM を含む村ではメイン text チャンネルの投稿も議論材料として扱います。

## 主な機能

- 固定配役 `人狼2 / 狂人1 / 占い師1 / 霊媒師1 / 騎士1 / 村人3` の 9 人村を Discord 上で進行できます。
- 人間プレイヤーが 9 人未満のとき、不足人数を Gnosia 風 persona の LLM プレイヤーで自動補完します。
- 秘密投票と夜行動を bot DM で受け付けます。
- 死者専用の `wolf-heaven` と、人狼専用の `wolf-wolves` を自動で作成します。
- メイン text チャンネルと秘密チャンネルの権限を進行に合わせて更新します。
- SQLite にゲーム状態を保存し、bot 再起動後は進行中ゲームの復旧を試みます。
- 締切時に未提出が残った場合は自動進行せず、ホスト判断待ちに入れます。

## 前提条件

- `Python 3.11`
- `uv`
- Discord Bot アプリケーション
- xAI API キー
- 既存のメイン text チャンネル
- 既存のメイン VC

## セットアップ

### 1. 依存関係をインストールする

```bash
uv sync
```

### 2. `.env` を用意する

`.env.example` をコピーして `.env` を作成し、各値を設定します。

```bash
cp .env.example .env
```

最初は次の内容を埋めれば起動に必要な最低限の設定が揃います。

```env
DISCORD_TOKEN=your_discord_bot_token
XAI_API_KEY=your_xai_api_key
XAI_MODEL=grok-4-1-fast
DISCORD_GUILD_ID=123456789012345678
MAIN_TEXT_CHANNEL_ID=123456789012345678
MAIN_VOICE_CHANNEL_ID=123456789012345678
WOLFBOT_DB_PATH=./wolfbot.db
LOG_LEVEL=INFO
```

- `DISCORD_TOKEN`: Discord Developer Portal で作成した bot のトークンを入れます。
- `XAI_API_KEY`: xAI の API キーを入れます。
- `DISCORD_GUILD_ID`: bot を動かす Discord サーバーの ID を入れます。
- `MAIN_TEXT_CHANNEL_ID`: 議論用に使うメイン text チャンネルの ID を入れます。
- `MAIN_VOICE_CHANNEL_ID`: プレイヤーが会話するメイン VC の ID を入れます。
- `XAI_MODEL`、`WOLFBOT_DB_PATH`、`LOG_LEVEL` は最初は既定値のままで構いません。

`DISCORD_GUILD_ID`、`MAIN_TEXT_CHANNEL_ID`、`MAIN_VOICE_CHANNEL_ID` にはチャンネル名や `#channel` のようなメンション文字列ではなく、数値の ID を設定してください。どの guild やチャンネルを使うか未定なら、先に手順 3 を済ませてから `.env` を埋めてください。

### 3. Discord 側の設定を済ませる

1. bot を運用する guild を 1 つ決めます。
2. その guild に、ゲーム専用で使う既存のメイン text チャンネルと既存のメイン VC を用意します。
3. メイン text チャンネルは、ゲーム外の投稿や観戦者の発言が混ざらないよう、専用チャンネルにするか基底権限を事前に確認します。
4. Discord Developer Portal で対象 bot の設定を開き、Privileged Gateway Intents の `Message Content Intent` と `Server Members Intent` を有効にします。
5. Discord クライアントで開発者モードを有効にし、guild とチャンネルの数値 ID をコピーできるようにします。
6. サーバー ID をコピーして `DISCORD_GUILD_ID` に入れます。
7. メイン text チャンネルの ID をコピーして `MAIN_TEXT_CHANNEL_ID` に入れます。
8. メイン VC の ID をコピーして `MAIN_VOICE_CHANNEL_ID` に入れます。
9. bot に少なくとも、テキストチャンネル作成/削除、チャンネル権限変更、メッセージ送信、チャンネル閲覧に必要な権限を付与します。
10. プレイヤーが bot から DM を受け取れる状態か確認します。`/wolf start` 実行時に DM を開けない参加者がいると開始できません。
11. `wolf-heaven` と `wolf-wolves` はゲーム作成時に bot が自動で作成し、ゲーム終了時に削除するため、管理者が事前に作る必要はありません。

### 4. bot を起動する

```bash
uv run wolfbot
```

起動後、`/wolf` コマンドが設定した guild に同期されます。

## 環境変数一覧

| 変数名 | 必須 / 既定値 | 用途 |
| --- | --- | --- |
| `DISCORD_TOKEN` | 必須 | Discord bot のトークン |
| `XAI_API_KEY` | 必須 | xAI API キー |
| `XAI_MODEL` | 既定値: `grok-4-1-fast` | 使用する xAI モデル名 |
| `DISCORD_GUILD_ID` | 必須 | `/wolf` コマンドを同期する guild の ID |
| `MAIN_TEXT_CHANNEL_ID` | 必須 | 議論用に使う既存のメイン text チャンネル ID |
| `MAIN_VOICE_CHANNEL_ID` | 必須 | 参加者が会話する既存のメイン VC の ID |
| `WOLFBOT_DB_PATH` | 既定値: `./wolfbot.db` | SQLite データベースの保存先 |
| `LOG_LEVEL` | 既定値: `INFO` | ログ出力レベル |

## Discord 側の準備

- メイン text チャンネルとメイン VC は、bot が新規作成するのではなく既存チャンネルを使います。
- slash command は `/wolf` グループで提供され、設定された 1 つの guild に同期されます。
- bot はゲーム作成時に秘密 text チャンネル `wolf-heaven` と `wolf-wolves` を自動作成し、ゲーム終了時に削除します。
- bot は進行に応じてチャンネル権限の上書きを更新します。
- メイン text チャンネルの基底権限と、プレイヤーの DM 受信設定は事前に管理者が確認してください。

## 使い方

1. ホストが `/wolf create` でゲームを作成します。
2. 参加者が `/wolf join` でロビーに入ります。開始前なら `/wolf leave` で退出できます。
3. ホストが `/wolf start` を実行すると、人数不足がある場合は LLM が補完されてゲームが始まります。
4. 進行中の状況確認には `/wolf status` を使います。
5. 締切時に未提出が残って止まった場合は、ホストが `/wolf extend` または `/wolf force-skip` で再開します。
6. 進行中のゲームを中止したい場合は、ホストまたは管理者が `/wolf abort` を使います。

## コマンド一覧

| コマンド | 役割 |
| --- | --- |
| `/wolf create` | 新しい 9 人村ゲームを作成し、秘密チャンネルを準備します。 |
| `/wolf join` | ロビー中のゲームに参加します。 |
| `/wolf leave` | ロビー中のゲームから退出します。 |
| `/wolf start` | ゲームを開始します。人数不足があると LLM を補完します。 |
| `/wolf status` | 現在のフェイズ、残り時間、参加者、生死、ホスト待ち情報を確認します。 |
| `/wolf extend` | ホスト判断待ち中の締切を延長します。 |
| `/wolf force-skip` | ホスト判断待ち中に未提出を確定扱いにして進行します。 |
| `/wolf abort` | ホストまたは管理者が進行中のゲームを強制終了します。 |

## ゲーム進行の要約

- ゲームは `LOBBY` から始まり、開始時に不足人数があれば LLM プレイヤーを自動補完します。
- 役職配布と初夜処理のあと、1 日目の昼議論に入ります。
- 以後は、昼議論、投票、必要なら決選投票、夜行動を繰り返します。
- 投票と夜行動は bot DM で行います。
- 死亡者は天国チャンネルを使い、人狼は夜に人狼専用チャットを使います。
- 締切を過ぎても未提出が残っている場合は自動で先に進まず、ホストが `/wolf extend` または `/wolf force-skip` を実行するまで待機します。

## 運用上の注意

- LLM は VC 音声を読みません。LLM を含む村では、重要な情報をメイン text チャンネルにも流してください。
- CO、占い結果、霊媒結果、質問、強い疑い、要約などは text に残しておく運用が前提です。
- VC の音声認識や自動文字起こしはありません。
- Web UI はありません。
- 9 人村以外の配役には対応していません。
- 実運用では、メインチャンネルの権限設定とプレイヤーの DM 受信設定が前提になります。

## 開発者向け補足

開発用の基本コマンドは以下です。

```bash
uv run pytest tests
uv run ruff check src tests
uv run ruff format src tests
uv run mypy
```

詳細な仕様や実装上の前提を確認したい場合は `CLAUDE.md` と `prompts/IMPLEMENTATION_PROMPT.md` を参照してください。
