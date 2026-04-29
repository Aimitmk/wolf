# wolfbot Game Viewer

Web UI for inspecting one wolfbot game's full event timeline + LLM internals after the fact. Reads exported game JSONs from `viewer/games/`.

## Quick start

```bash
cd viewer
pnpm install
pnpm dev
# → http://localhost:3000
```

## Routes

| Path | Purpose |
|---|---|
| `/` | **ゲーム一覧** — table of every export in `viewer/games/`, newest first. Click a row to drill in. |
| `/games/{game_id}` | Detail view: seats, phase timeline, LLM trace drawer, stats |
| `/sample` | Bundled synthetic sample game — for trying the UI without playing a real game |

## Where the data comes from

`viewer/games/{game_id}.json` is written **automatically** at the end of every game (natural victory or `/wolf abort`) by the bot's `GameService._on_game_end_finalize` hook. Each file is the join of:

- `wolfbot.db` — game/seats/public_logs/votes/night_actions/speech_events
- `logs/llm_calls/{game_id}/*.jsonl` — every LLM call's prompt, response, tokens, latency

So the typical flow is:

```bash
# 1. Run the bot, play a game (real or with --mock)
scripts/run-bots.sh
# /wolf join, /wolf start, ... → game ends or you /wolf abort

# 2. Open the viewer; the just-finished game shows up at the top
cd viewer && pnpm dev
```

No manual export step. If you want to re-export an older game from SQLite + JSONL into the viewer, run:

```bash
uv run python scripts/export-game.py --game-id g_abc123def
```

## Running with the sample game

The sample game is **opt-in** — visit `/sample` (linked from the empty-state and from a button in the upper-right of `/`).

It's a hand-crafted 2-day village victory designed to exercise every UI surface. Generate / regenerate it with:

```bash
uv run python viewer/sample-data/generate_sample.py
```

## What gets shown on a detail page

- **Header** — game id, mode, victor, total LLM calls / tokens / latency
- **Seat grid** — per-seat card with role, persona, alive/dead status, faction tint
- **Phase timeline** — each phase: public logs, speeches (CO badge / STT confidence), votes, night actions, sorted chronologically. Bulb icon opens the LLM trace drawer for that event.
- **Trace drawer** — system + user prompt, raw response, prompt/completion/total tokens, latency, model, error
- **Stats panel** — per-seat and per-phase aggregates (calls / tokens / latency)

## Schema

The canonical TypeScript shape lives in [`src/lib/types.ts`](./src/lib/types.ts). The Python writer that produces it is [`wolfbot.services.game_export`](../src/wolfbot/services/game_export.py); the test fixture asserting the shape is [`tests/test_game_export.py`](../tests/test_game_export.py).
