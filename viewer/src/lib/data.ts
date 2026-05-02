import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { buildMatchMaps, type MatchMaps } from "./match";
import type { DiscussionMode, GameSample, TraceEntry } from "./types";

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
  /** Number of seats whose ``is_llm`` is false (i.e., real Discord users). */
  human_count: number;
  /** Number of seats whose ``is_llm`` is true (NPC personas). */
  llm_count: number;
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

/**
 * Load one exported game by id, or ``null`` if the file is missing.
 *
 * The bot writes files as ``YYYY-MM-DD_HH-MM-SS.json`` (sortable by play
 * time, see ``services/game_export.py``) — the file name does **not**
 * encode the internal ``game.id``. To resolve a link, fast-path on
 * ``{gameId}.json`` (legacy / forward-compat) and otherwise scan the
 * directory for a JSON whose ``game.id`` matches.
 */
export async function loadGameById(
  gameId: string,
): Promise<GameSample | null> {
  // Defensive — never resolve outside the games dir even if a path-traversal
  // id slips in.
  if (gameId.includes("/") || gameId.includes("..")) return null;
  const exportsDir = path.resolve(process.cwd(), GAMES_DIR);

  // Fast path — legacy ``{id}.json`` layout.
  const legacy = path.join(exportsDir, `${gameId}.json`);
  try {
    const raw = await readFile(legacy, "utf-8");
    return JSON.parse(raw) as GameSample;
  } catch {
    // fall through to directory scan
  }

  // Directory scan — match ``game.id`` inside each JSON.
  let names: string[];
  try {
    names = await readdir(exportsDir);
  } catch {
    return null;
  }
  const candidates = names.filter((n) => n.endsWith(".json"));
  for (const name of candidates) {
    const fullPath = path.join(exportsDir, name);
    try {
      const raw = await readFile(fullPath, "utf-8");
      const data = JSON.parse(raw) as GameSample;
      if (data.game.id === gameId) {
        return data;
      }
    } catch {
      // skip unreadable / malformed files
    }
  }
  return null;
}

/** Load the bundled sample game (opt-in via the ``/sample`` route). */
export async function loadSample(): Promise<GameSample> {
  const target = path.resolve(process.cwd(), SAMPLE_PATH);
  const raw = await readFile(target, "utf-8");
  return JSON.parse(raw) as GameSample;
}

/**
 * What `loadGameWithMatches` returns.
 *
 * `data.trace[i]` has the heavy fields (`system_prompt`, `user_prompt`,
 * `response`) replaced by empty strings / null. The full versions are
 * available via the `/api/games/[gameId]/trace/[index]` route. This
 * trims hundreds of KB to several MB off the SSR HTML payload for
 * games with non-trivial trace counts — historically the dominant
 * cost of opening a game detail page.
 *
 * `matches.trace[eventKey]` returns the index into `data.trace` (i.e.
 * the position of the matched trace entry). PhaseSection consumes this
 * map and never re-runs the matchers client-side. `matches.arbiter`
 * mirrors the same shape for speech → arbiter dispatch lookup.
 *
 * Returns `null` when the game JSON does not exist.
 */
export interface GameWithMatches {
  data: GameSample;
  matches: MatchMaps;
}

export async function loadGameWithMatches(
  gameId: string,
): Promise<GameWithMatches | null> {
  const data = await loadGameById(gameId);
  if (data === null) return null;
  // Run the matchers BEFORE slimming so they see the heavy fields.
  const matches = buildMatchMaps(
    data.phases,
    data.trace,
    data.arbiter_decisions ?? [],
    data.seats,
  );
  return { data: { ...data, trace: slimTrace(data.trace) }, matches };
}

/**
 * Heavy-field accessor used by the lazy-load API route. Re-reads the
 * source JSON because slimmed copies don't keep the original strings
 * around — the cost of one cold disk read per drawer-open is dwarfed
 * by the SSR payload savings on every page load.
 */
export interface TraceHeavyFields {
  system_prompt: string;
  user_prompt: string;
  response: string | null;
}

export async function loadTraceHeavyFields(
  gameId: string,
  index: number,
): Promise<TraceHeavyFields | null> {
  const data = await loadGameById(gameId);
  if (data === null) return null;
  if (!Number.isInteger(index) || index < 0 || index >= data.trace.length) {
    return null;
  }
  const t = data.trace[index];
  return {
    system_prompt: t.system_prompt,
    user_prompt: t.user_prompt,
    response: t.response,
  };
}

function slimTrace(trace: TraceEntry[]): TraceEntry[] {
  // Replace heavy strings with empty placeholders rather than
  // ``undefined`` so the client type stays the same and existing
  // ``entry.system_prompt`` accesses stay valid until they explicitly
  // opt into the lazy fetch.
  return trace.map((t) => ({
    ...t,
    system_prompt: "",
    user_prompt: "",
    response: t.response === null ? null : "",
  }));
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
  let humanCount = 0;
  let llmCount = 0;
  for (const seat of data.seats) {
    if (seat.is_llm) {
      llmCount += 1;
    } else {
      humanCount += 1;
    }
  }
  return {
    id: data.game.id,
    discussion_mode: data.game.discussion_mode,
    victory: data.game.victory,
    created_at_ms: data.game.created_at_ms,
    ended_at_ms: data.game.ended_at_ms,
    duration_ms: duration,
    seat_count: data.seats.length,
    human_count: humanCount,
    llm_count: llmCount,
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
