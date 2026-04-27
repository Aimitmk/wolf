# NPC bot env templates

各ペルソナごとに 1 つのテンプレ (`.env.<persona>.example`) がある。NPC bot は
**1 プロセス = 1 ペルソナ**なので、立ち上げたいペルソナぶんコピーして使う。

## ファイルの意味

| パターン | 意味 | git |
|---|---|---|
| `envs/npc/.env.<persona>.example` | テンプレ (秘密値は空欄) | コミットされる |
| `envs/npc/.env.<persona>` | 実 env (秘密値を埋めたもの) | gitignore |

## 使い方

```bash
# 1) テンプレをコピー
cp envs/npc/.env.setsu.example envs/npc/.env.setsu

# 2) 編集して秘密値を埋める
#   NPC_DISCORD_TOKEN     ── このペルソナ専用の Discord bot トークン
#   DISCORD_GUILD_ID      ── Master と同じ guild ID
#   MAIN_VOICE_CHANNEL_ID ── Master と同じ VC ID
#   MASTER_NPC_PSK        ── Master の MASTER_NPC_PSK と同値
#   NPC_LLM_API_KEY       ── NPC LLM の API キー
#                            (Master の GAMEPLAY_LLM_API_KEY と共用可)
$EDITOR envs/npc/.env.setsu

# 3) 起動 (env ファイルは WOLFBOT_NPC_ENV で指定)
WOLFBOT_NPC_ENV=envs/npc/.env.setsu uv run wolfbot-npc

# 4) 別ペルソナを増やす場合は別プロセスで
WOLFBOT_NPC_ENV=envs/npc/.env.gina uv run wolfbot-npc &
WOLFBOT_NPC_ENV=envs/npc/.env.sq   uv run wolfbot-npc &
```

`reactive_voice` モードで `/wolf start` するとき、Master は
**online な NPC bot のうち未割当のものを席に充てる**。出したい
ペルソナのプロセスだけを起動しておけば、それだけが選ばれる。

## ペルソナ一覧 (テンプレで既定値を割り当て済み)

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

`TTS_VOICE_ID` は VOICEVOX のスピーカー ID。重複しないように既定で
割り当ててあるが、好みの声に変えたい場合だけ編集する
(<https://voicevox.hiroshiba.jp/> で確認)。

## 事前にやっておくこと

- 各ペルソナぶんの Discord bot アプリを Developer Portal で作成し、
  guild に**手動で招待**しておく (Discord は bot から bot を guild に
  追加できないので、これは 1 度きりのセットアップ作業)。
- Master の `.env.master` を先に用意し、`MASTER_NPC_PSK` を決めておく。
- VOICEVOX エンジンを立ち上げておく (既定 `http://localhost:50021`)。
