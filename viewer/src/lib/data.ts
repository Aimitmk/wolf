import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import type { DiscussionMode, GameSample } from "./types";

/**
 * Where exported game JSONs live.  Populated automatically by the bot's
 * ``GameService._on_game_end_finalize`` hook at the end of every game
 * (victory or ``/wolf abort``).
 *
 * The viewer treats this directory as its database — the list page
 * scans it; detail pages read a specific file from it.
 */
const GAMES_DIR = "games";
const SAMPLE_PATH = path.join("sample-data", "game-sample.json");

/** One row in the top-level games-list table. */
export interface GameSummary {
  id: string;
  discussion_mode: DiscussionMode;
  victory: "village" | "wolf" | null;
  created_at_ms: number;
  ended_at_ms: number | null;
  duration_ms: number | null;
  seat_count: number;
  llm_call_count: number;
  total_tokens: number;
  total_latency_ms: number;
  file_mtime_ms: number;
}

/** List every exported game, newest first by mtime. */
export async function listGames(): Promise<GameSummary[]> {
  const exportsDir = path.resolve(process.cwd(), GAMES_DIR);
  let names: string[];
  try {
    names = await readdir(exportsDir);
  } catch {
    return [];
  }
  const jsonFiles = names.filter((n) => n.endsWith(".json"));
  if (jsonFiles.length === 0) return [];

  const summaries = await Promise.all(
    jsonFiles.map(async (name) => {
      const fullPath = path.join(exportsDir, name);
      try {
        const [raw, st] = await Promise.all([
          readFile(fullPath, "utf-8"),
          stat(fullPath),
        ]);
        const data = JSON.parse(raw) as GameSample;
        return summarize(data, st.mtimeMs);
      } catch {
        return null;
      }
    }),
  );
  return summaries
    .filter((s): s is GameSummary => s !== null)
    .sort((a, b) => b.file_mtime_ms - a.file_mtime_ms);
}

/** Load one exported game by id, or ``null`` if the file is missing. */
export async function loadGameById(
  gameId: string,
): Promise<GameSample | null> {
  // Defensive — never resolve outside the games dir even if a path-traversal
  // id slips in.
  if (gameId.includes("/") || gameId.includes("..")) return null;
  const target = path.resolve(process.cwd(), GAMES_DIR, `${gameId}.json`);
  try {
    const raw = await readFile(target, "utf-8");
    return JSON.parse(raw) as GameSample;
  } catch {
    return null;
  }
}

/** Load the bundled sample game (opt-in via the ``/sample`` route). */
export async function loadSample(): Promise<GameSample> {
  const target = path.resolve(process.cwd(), SAMPLE_PATH);
  const raw = await readFile(target, "utf-8");
  return JSON.parse(raw) as GameSample;
}

function summarize(data: GameSample, mtimeMs: number): GameSummary {
  let totalTokens = 0;
  let totalLatencyMs = 0;
  for (const t of data.trace) {
    totalLatencyMs += t.latency_ms;
    totalTokens += t.tokens?.total ?? 0;
  }
  const duration =
    data.game.ended_at_ms != null
      ? data.game.ended_at_ms - data.game.created_at_ms
      : null;
  return {
    id: data.game.id,
    discussion_mode: data.game.discussion_mode,
    victory: data.game.victory,
    created_at_ms: data.game.created_at_ms,
    ended_at_ms: data.game.ended_at_ms,
    duration_ms: duration,
    seat_count: data.seats.length,
    llm_call_count: data.trace.length,
    total_tokens: totalTokens,
    total_latency_ms: totalLatencyMs,
    file_mtime_ms: mtimeMs,
  };
}

/** Stable phase key suitable for React keys + URL hashes. */
export function phaseKey(day: number, phase: string): string {
  return `d${day}-${phase}`;
}
