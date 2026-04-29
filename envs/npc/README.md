# NPC bot env files

NPC bot は **1 プロセス = 1 ペルソナ** で動きます。各ペルソナの env ファイル
(`envs/npc/.env.<persona>`) は手書きせず、テンプレと `tokens.txt` から
**生成スクリプトで自動作成**します。

## 構成

| ファイル | 意味 | git |
|---|---|---|
| `envs/npc/.env.npc.example` | 単一テンプレ (`{{...}}` プレースホルダ) | コミットされる |
| `envs/npc/.env.<persona>` | 生成された実 env (秘密値を含む) | gitignored |
| `tokens.txt` (リポジトリルート) | `<persona>: <token>` の一覧 | gitignored |

## セットアップ

### 1. tokens.txt を用意

リポジトリルートに `tokens.txt` を作り、Discord Developer Portal で発行した
NPC bot のトークンを 1 行 1 ペルソナで貼る:

```
master: <Master bot のトークン>          ← 任意 (生成時はスキップされる)
setsu:  <セツ bot のトークン>
gina:   <ジナ bot のトークン>
sq:     <SQ bot のトークン>
raqio:  <ラキオ bot のトークン>
...
```

- 行の先頭が `#` ならコメントとして無視される
- `master` 行は **Master 用なので生成対象から除外** される (Master のトークンは
  `.env.master` に書く)
- `setu` のような typo は `setsu` に自動マッピングされる (`PERSONA_ALIASES` で
  定義済み)

### 2. .env.master を先に用意

生成スクリプトは以下の値を `.env.master` から拾うので、先に値を埋めておく:

- `DISCORD_GUILD_ID`
- `MAIN_VOICE_CHANNEL_ID`
- `MASTER_NPC_PSK`
- `GAMEPLAY_LLM_API_KEY` (各 NPC の `NPC_LLM_API_KEY` として再利用される)

### 3. 生成スクリプトを実行

```bash
# 全ペルソナぶん生成
python3 scripts/generate_npc_envs.py

# 既存ファイルを上書きしたくない
python3 scripts/generate_npc_envs.py --no-overwrite

# 実際には書かず内容だけ確認
python3 scripts/generate_npc_envs.py --dry-run

# 入力パスを変えたい
python3 scripts/generate_npc_envs.py --tokens path/to/tokens.txt --master path/to/.env.master
```

`tokens.txt` を更新したり `.env.master` の共有値を変えたりしたら**再実行**で
全 NPC env を一括更新できます。手書き編集はやめて、編集はテンプレ側に集約。

### 4. 起動

```bash
WOLFBOT_NPC_ENV=envs/npc/.env.setsu uv run wolfbot-npc
```

複数まとめて起動したいときは `scripts/run-bots.sh` (tmux で 1 ウィンドウ = 1 bot)。

## ペルソナ一覧 (canonical source: `wolfbot.npc.personas`)

| Key | 表示名 | スタイル | TTS_VOICE_ID |
|---|---|---|---|
| `setsu` | 🟡セツ | 真面目で責任感が強い。議論を整理する | 8 |
| `gina` | 🟣ジナ | 物静かで誠実。直感と共感を重視 | 9 |
| `sq` | 🔴SQ | 軽快で社交的。打算的な面もありつつ場を和ませる | 2 |
| `raqio` | 🦋ラキオ | 論理偏重で挑発的。矛盾追及が鋭い | 13 |
| `stella` | 🌟ステラ | 優しく献身的。柔らかい物言い | 4 |
| `shigemichi` | 👽シゲミチ | 率直で豪快。印象や勢いを重視 | 11 |
| `chipie` | 🐈‍⬛シピ | 柔らかく観察力がある。対立をなだめつつ疑問を出す | 6 |
| `comet` | ☄️コメット | 無邪気で気まぐれ。妙に核心を突く | 1 |
| `jonas` | 🎩ジョナス | 尊大で芝居がかった話し方 | 12 |
| `kukrushka` | 🧸ククルシカ | 不穏。ほぼ無言で身振りを示す | 0 |
| `otome` | 🐬オトメ | 事務的で面倒見がよい。要点中心 | 7 |
| `sha_ming` | 🥽シャーミン | 皮肉屋で自信家。挑発的に試す | 5 |
| `remnan` | ⚪️レムナン | 内向的で慎重。観察は細かい | 10 |
| `yuriko` | 👑ユリコ | 冷静で威圧感がある。少ない語数で核心を突く | 3 |

`TTS_VOICE_ID` は `wolfbot.npc.personas` の `Persona.tts_voice_id` が一次情報。
別の声に変えたい場合は (a) Persona 定義を編集してスクリプトを再実行、または
(b) 生成された `.env.<persona>` を後から手動で書き換える。

## 事前にやっておくこと

- 各ペルソナぶんの Discord bot アプリを Developer Portal で作成し、
  guild に手動で招待しておく (1 度きりのセットアップ作業)。
- VOICEVOX エンジンを起動 (既定 `http://localhost:50021`)。
