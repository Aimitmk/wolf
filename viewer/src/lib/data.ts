import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import type { GameSample } from "./types";

/**
 * Resolve which JSON file to display, in priority order:
 *
 * 1. ``GAME_FILE`` env var (absolute or relative to ``viewer/``)
 * 2. The most-recently-modified ``*.json`` in ``viewer/games/``
 *    (populated automatically by ``GameService._on_game_end_finalize``)
 * 3. Fallback: ``viewer/sample-data/game-sample.json``
 *
 * The intent: after a real game ends, ``cd viewer && pnpm dev`` Just
 * Works — the bot has already written the export to ``viewer/games/``,
 * the viewer auto-picks the newest. No env var, no manual export step.
 *
 * Server-only — must NEVER be called from a ``"use client"`` component.
 */
export async function loadGame(): Promise<GameSample> {
  const target = await resolveGameFile();
  const raw = await readFile(target, "utf-8");
  return JSON.parse(raw) as GameSample;
}

async function resolveGameFile(): Promise<string> {
  const override = process.env.GAME_FILE;
  if (override) {
    return path.isAbsolute(override)
      ? override
      : path.resolve(process.cwd(), override);
  }
  const recent = await pickMostRecentExport();
  if (recent) return recent;
  return path.resolve(process.cwd(), "sample-data", "game-sample.json");
}

async function pickMostRecentExport(): Promise<string | null> {
  const exportsDir = path.resolve(process.cwd(), "games");
  let names: string[];
  try {
    names = await readdir(exportsDir);
  } catch {
    return null;
  }
  const jsonFiles = names.filter((n) => n.endsWith(".json"));
  if (jsonFiles.length === 0) return null;

  const stats = await Promise.all(
    jsonFiles.map(async (name) => {
      const p = path.join(exportsDir, name);
      const s = await stat(p);
      return { path: p, mtimeMs: s.mtimeMs };
    }),
  );
  stats.sort((a, b) => b.mtimeMs - a.mtimeMs);
  return stats[0]!.path;
}

/** Return a stable phase key suitable for React keys + URL hashes. */
export function phaseKey(day: number, phase: string): string {
  return `d${day}-${phase}`;
}
