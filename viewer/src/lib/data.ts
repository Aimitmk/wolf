import { readFile } from "node:fs/promises";
import path from "node:path";
import type { GameSample } from "./types";

/**
 * Resolve the JSON file the viewer should display.
 *
 * Default: `viewer/sample-data/game-sample.json`.
 * Override at launch time with `GAME_FILE=/abs/or/relative/path.json`.
 *
 * Server-only — must NEVER be called from a "use client" component.
 */
export async function loadGame(): Promise<GameSample> {
  const override = process.env.GAME_FILE;
  const target = override
    ? path.isAbsolute(override)
      ? override
      : path.resolve(process.cwd(), override)
    : path.resolve(process.cwd(), "sample-data", "game-sample.json");

  const raw = await readFile(target, "utf-8");
  return JSON.parse(raw) as GameSample;
}

/** Return a stable phase key suitable for React keys + URL hashes. */
export function phaseKey(day: number, phase: string): string {
  return `d${day}-${phase}`;
}
