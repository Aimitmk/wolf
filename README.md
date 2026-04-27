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
- (任意) `tmux` — `scripts/run-bots.sh` で複数 bot をまとめて起動する場合のみ。macOS は `brew install tmux`。

## セットアップ

### 1. 依存関係をインストールする

```bash
uv sync
```

### 2. `.env.master` を用意する

`.env.master.example` をコピーして `.env.master` を作成し、Master bot 用の値を設定します。

```bash
cp .env.master.example .env.master
```

reactive_voice モードで NPC bot も動かす場合は、**1 ペルソナ = 1 プロセス = 1 Discord bot アカウント** で別途用意します。各ペルソナの env ファイルは手書きせず、リポジトリルートの `tokens.txt` (gitignored) と単一テンプレ `envs/npc/.env.npc.example` から自動生成します。

```bash
# 1) tokens.txt にトークンを 1 行 1 ペルソナで貼る (リポジトリルートに作る)
#    例:
#      setsu:  MTQ5...
#      gina:   MTQ5...
#      sq:     MTQ5...
#      ...

# 2) 生成 (envs/npc/.env.<persona> がペルソナぶんできる)
python3 scripts/generate_npc_envs.py

# 3) tmux で Master + 起動済みの全 NPC を一括起動
scripts/run-bots.sh
```

各 NPC bot のセットアップ詳細・ペルソナ一覧・TTS_VOICE_ID の既定値は [envs/npc/README.md](envs/npc/README.md) を参照してください。

各 NPC bot は固有のペルソナ (`NPC_PERSONA_KEY`) を起動時に確定します。Master の `/wolf start` (reactive_voice モード) は、online な NPC bot の中から不足席数だけ選び、その bot の persona を席に割り当てます。online な bot が足りないときは開始時にエラー表示します (rounds モードに切り替えるか、bot を増やすことで解決)。

最初は `.env.master` に次の内容を埋めれば、Master 単体で起動に必要な最低限の設定が揃います。

```env
DISCORD_TOKEN=your_discord_bot_token
GAMEPLAY_LLM_API_KEY=your_gameplay_llm_api_key
GAMEPLAY_LLM_MODEL=grok-4-1-fast
DISCORD_GUILD_ID=123456789012345678
MAIN_TEXT_CHANNEL_ID=123456789012345678
MAIN_VOICE_CHANNEL_ID=123456789012345678
WOLFBOT_DB_PATH=./wolfbot.db
LOG_LEVEL=INFO
```

- `DISCORD_TOKEN`: Discord Developer Portal で作成した bot のトークンを入れます。
- `GAMEPLAY_LLM_API_KEY`: Gameplay LLM (Master が投票・夜行動・rounds-mode 議論文を判断する LLM) の API キーを入れます。OpenAI Chat Completions 互換のプロバイダなら何でも可。
- `DISCORD_GUILD_ID`: bot を動かす Discord サーバーの ID を入れます。
- `MAIN_TEXT_CHANNEL_ID`: 議論用に使うメイン text チャンネルの ID を入れます。
- `MAIN_VOICE_CHANNEL_ID`: プレイヤーが会話するメイン VC の ID を入れます。
- `GAMEPLAY_LLM_MODEL`、`WOLFBOT_DB_PATH`、`LOG_LEVEL` は最初は既定値のままで構いません。

`DISCORD_GUILD_ID`、`MAIN_TEXT_CHANNEL_ID`、`MAIN_VOICE_CHANNEL_ID` にはチャンネル名や `#channel` のようなメンション文字列ではなく、数値の ID を設定してください。どの guild やチャンネルを使うか未定なら、先に手順 3 を済ませてから `.env` を埋めてください。

### 3. Discord 側の設定を済ませる

1. bot を運用する guild を 1 つ決めます。
2. その guild に、ゲーム専用で使う既存のメイン text チャンネルと既存のメイン VC を用意します。
3. メイン text チャンネルは、ゲーム外の投稿や観戦者の発言が混ざらないよう、専用チャンネルにするか基底権限を事前に確認します。
4. Discord Developer Portal で対象 app の `Bot` ページを開き、Privileged Gateway Intents の `Message Content Intent` と `Server Members Intent` を有効にします。
5. 同じ `Bot` ページで `Require OAuth2 Code Grant` が有効なら無効にします。これが有効だと、通常の `Add to server` フローだけでは bot が guild に参加しません。owner 以外にも追加させたい場合は `Public Bot` も有効にしておきます。
6. `Installation` ページを開き、`Install Link` が `Discord Provided Link` になっていることを確認します。
7. `Installation Contexts` で `Guild Install` を有効にします。
8. `Default Install Settings` の `Guild Install` 側で、scopes に `bot` と `applications.commands` の両方を入れます。`applications.commands` だけでは slash command app としては入っても、bot メンバーとして guild に参加しません。
9. bot に次の権限を付与します。

   | 権限名 | 必須 / 補足 | 用途 |
   | --- | --- | --- |
   | `View Channels` | 必須 | メイン text、`wolf-heaven`、`wolf-wolves` を閲覧するため |
   | `Send Messages` | 必須 | 進行案内、朝の通知、ホスト待ち通知、復旧通知を送るため |
   | `Manage Channels` | 必須 | `wolf-heaven` と `wolf-wolves` を作成・削除するため |
   | `Manage Roles` | 必須 | チャンネルごとの permission overwrite を更新するため |
   | `Administrator` | 補足 | 初回の動作確認や切り分けには使えますが、常用は推奨しません |

   `Manage Roles` は Discord の画面によって `Manage Permissions` のように表示されることがあります。`Send Messages` がなければ公開チャンネルへの進行通知ができず、`View Channels` がなければチャンネル自体にアクセスできません。

   この bot は VC に参加したり発話したりしないため、音声系の権限は不要です。DM 送信は guild 権限ではなく各プレイヤーの DM 受信設定に依存します。
10. `Installation` ページの default Install Link を開き、`Add to server` を選んで対象 guild にインストールします。server に bot を入れたい場合は `Add to my apps` ではなく `Add to server` を選んでください。
11. インストール後、対象 guild のメンバー一覧に bot が表示されることを確認します。
12. メンバー一覧に bot がいない場合は `Server Settings > Integrations` を確認します。app は見えるのに bot メンバーがいない場合は、`Guild Install` や `bot` scope の設定がずれている可能性が高いので、Developer Portal の `Installation` ページを見直してから入れ直します。
13. Discord クライアントで開発者モードを有効にし、guild とチャンネルの数値 ID をコピーできるようにします。
14. サーバー ID をコピーして `DISCORD_GUILD_ID` に入れます。
15. メイン text チャンネルの ID をコピーして `MAIN_TEXT_CHANNEL_ID` に入れます。
16. メイン VC の ID をコピーして `MAIN_VOICE_CHANNEL_ID` に入れます。
17. プレイヤーが bot から DM を受け取れる状態か確認します。`/wolf start` 実行時に DM を開けない参加者がいると開始できません。
18. `wolf-heaven` と `wolf-wolves` はゲーム作成時に bot が自動で作成し、ゲーム終了時に削除するため、管理者が事前に作る必要はありません。

### 4. bot を起動する

```bash
uv run wolfbot
```

起動後、`/wolf` コマンドが設定した guild に同期されます。

### 5. (任意) Master + 複数 NPC bot を tmux でまとめて起動

reactive_voice モードで Master と複数の NPC bot を一度に起動したい場合、`scripts/run-bots.sh` が tmux で 1 ウィンドウ = 1 bot のセッションを作ります。macOS 前提 (Linux でも動作)、`tmux` が必要 (`brew install tmux`)。

```bash
# envs/npc/.env.<persona> が存在するペルソナを自動検出して全部起動
scripts/run-bots.sh

# 個別指定したい場合
scripts/run-bots.sh setsu gina sq raqio

# 既に同名セッションがあるときは停止して作り直す
FORCE=1 scripts/run-bots.sh

# セッションに入って各ウィンドウのログを見る
tmux attach -t wolfbot
#   prefix + n / p          : 次/前ウィンドウ
#   prefix + 0..9 / w       : 番号 / 一覧から選択
#   prefix + d              : デタッチ (bot は動き続ける)

# 全部止める
scripts/stop-bots.sh
```

各 bot のログは tmux ペインと `logs/<persona>.log` の両方にストリームされます。プロセスが落ちてもウィンドウは残るので、終了直後の出力を確認できます。

**人間 0 / NPC bot 9 体だけのゲーム**を観戦したい場合: `envs/npc/.env.<persona>` を 9 ペルソナぶん用意して `scripts/run-bots.sh` で全部起動 → Discord で `/wolf create` → `/wolf join` は打たず `/wolf start` で人数不足の 9 席すべてが NPC bot で埋まります。

## 環境変数一覧

### Master (`.env.master`, `wolfbot.config.MasterSettings`)

| 変数名 | 必須 / 既定値 | 用途 |
| --- | --- | --- |
| `DISCORD_TOKEN` | 必須 | Master bot のトークン |
| `DISCORD_GUILD_ID` | 必須 | `/wolf` コマンドを同期する guild の ID |
| `MAIN_TEXT_CHANNEL_ID` | 必須 | 議論用に使う既存のメイン text チャンネル ID |
| `MAIN_VOICE_CHANNEL_ID` | 必須 | 参加者が会話する既存のメイン VC の ID |
| `WOLFBOT_DB_PATH` | 既定値: `./wolfbot.db` | SQLite データベースの保存先 |
| `LOG_LEVEL` | 既定値: `INFO` | ログ出力レベル |
| `LLM_DISCUSSION_MODE` | 既定値: `rounds` | LLM 議論モード (`rounds` / `reactive_voice`) |
| `MASTER_WS_LISTEN` | 既定値: `127.0.0.1:8800` | Master ↔ NPC/voice-ingest WS の listen アドレス |
| `MASTER_NPC_PSK` | 任意 | NPC bot / voice-ingest の WS 認証用 Pre-Shared Key |
| `GAMEPLAY_LLM_API_KEY` | 必須 | **Gameplay LLM** — Master が LLM 席の挙動を判断するときに使う LLM。投票判断・夜行動 (襲撃/占い/護衛) は全モードで使用、議論ターン文の生成は rounds モードのみ (reactive_voice モードでは NPC bot が代わりに発話する)。OpenAI Chat Completions 互換のプロバイダなら何でも可 |
| `GAMEPLAY_LLM_MODEL` | 既定値: `grok-4-1-fast` | Gameplay LLM のモデル名 |
| `VOICE_LLM_API_KEY` | 任意 | **Voice LLM** — Master が VC で人間音声を聞いて書き起こし+構造化解析する multimodal LLM。reactive_voice モードでのみ使用 |
| `VOICE_LLM_MODEL` | 既定値: `gemini-2.0-flash-lite` | Voice LLM のモデル名 (multimodal audio input 対応) |

### NPC bot (`envs/npc/.env.<persona>`, `wolfbot.npc.config.NpcSettings`)

各 NPC bot は **固有 persona に紐付いた 1 プロセス**。[envs/npc/](envs/npc/) 配下の `.env.<persona>.example` をコピーして使います。プロセス起動時に `WOLFBOT_NPC_ENV` でファイルパスを指定 (例: `WOLFBOT_NPC_ENV=envs/npc/.env.setsu`)。

| 変数名 | 必須 / 既定値 | 用途 |
| --- | --- | --- |
| `NPC_ID` | 必須 (テンプレで `npc_<persona>` 既定) | NPC の一意 ID (Master WS 上の識別子) |
| `NPC_DISCORD_TOKEN` | 必須 | この persona 専用の Discord bot トークン |
| `NPC_PERSONA_KEY` | 必須 (テンプレで指定済) | Persona キー (`setsu` / `gina` / `sq` / `raqio` / `stella` / `shigemichi` / `chipie` / `comet` / `jonas` / `kukrushka` / `otome` / `sha_ming` / `remnan` / `yuriko`) |
| `DISCORD_GUILD_ID` | 必須 | Master と同じ guild ID |
| `MAIN_VOICE_CHANNEL_ID` | 必須 | Master と同じ VC の ID |
| `MASTER_WS_URL` | 既定値: `ws://127.0.0.1:8800` | Master WS への接続先 URL |
| `MASTER_NPC_PSK` | 必須 | Master の `MASTER_NPC_PSK` と同値 |
| `NPC_LLM_API_KEY` | 必須 | **NPC LLM** — この NPC bot が VC で 1 行発言を生成するときに使う LLM。投票・夜行動の判断はしない (Master の Gameplay LLM が担当)。Master の `GAMEPLAY_LLM_API_KEY` と共用しても、別プロバイダのキーでもよい |
| `NPC_LLM_MODEL` | 既定値: `grok-4-1-fast` | NPC LLM のモデル名 (プロバイダに合わせて変える) |
| `NPC_LLM_BASE_URL` | 既定値: `https://api.x.ai/v1` | OpenAI Chat Completions 互換エンドポイント。プロバイダ切り替えはここを変える |
| `TTS_VOICE_ID` | テンプレで persona 別に既定値設定 | VOICEVOX のスピーカー ID。好みの声に変えたい場合のみ編集 |
| `VOICEVOX_URL` | 既定値: `http://localhost:50021` | VOICEVOX エンジンの URL |
| `HEARTBEAT_INTERVAL_S` | 既定値: `5` | ハートビート送信間隔(秒) |
| `LOG_LEVEL` | 既定値: `INFO` | ログ出力レベル |

## Discord 側の準備

- メイン text チャンネルとメイン VC は、bot が新規作成するのではなく既存チャンネルを使います。
- slash command は `/wolf` グループで提供され、設定された 1 つの guild に同期されます。
- bot を guild へ入れるときは `Add to server` を選びます。`Add to my apps` は user install で、server member としては参加しません。
- bot はゲーム作成時に秘密 text チャンネル `wolf-heaven` と `wolf-wolves` を自動作成し、ゲーム終了時に削除します。
- bot は進行に応じてチャンネル権限の上書きを更新します。必要な権限はセットアップ手順 3 の表を参照してください。
- `Server Settings > Integrations` にだけ app が見えて bot メンバーがいない場合は、`Installation` ページの `Guild Install` と `bot` scope を見直してください。
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
- `/wolf create` 実行中に bot プロセスが強制終了した場合、Discord 上に `wolf-heaven` / `wolf-wolves` のチャンネルが orphan として残ることがあります。bot は安全のため次回 `/wolf create` を「同名チャンネルが既に存在する」エラーで止めるので、Discord 上で該当チャンネルを手動削除してから再実行してください (DB エラーなど通常の例外で create が失敗した場合は bot 側で自動クリーンアップされます)。

## 開発者向け補足

開発用の基本コマンドは以下です。

```bash
uv run pytest tests
uv run ruff check src tests
uv run ruff format src tests
uv run mypy
```

詳細な仕様や実装上の前提を確認したい場合は `CLAUDE.md` と `prompts/IMPLEMENTATION_PROMPT.md` を参照してください。
