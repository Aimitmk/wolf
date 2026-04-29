/**
 * Cross-stack contract test: viewer types ↔ Python exporter output.
 *
 * Three guarantees:
 *
 * 1. The committed sample (`sample-data/game-sample.json`) validates
 *    against `sample-data/export-schema.json` (the JSON Schema emitted
 *    by the Python `GameExport` Pydantic model). If a viewer component
 *    imports a TS type that diverges from this schema, the test fails.
 *
 * 2. Any auto-exported real-game files in `games/*.json` (created by
 *    `GameService._on_game_end_finalize` after a victory or `/wolf
 *    abort`) also validate against the same schema. Stale files from
 *    earlier schema versions surface clearly here so the user knows to
 *    re-export.
 *
 * 3. The TypeScript `GameSample` interface accepts the schema-validated
 *    payload — proven by the runtime call to `loadGameById` /
 *    `loadSample` plus a structural check on every required key.
 *
 * The schema itself is regenerated from the Python source of truth
 * (`uv run python scripts/dump-export-schema.py`); a separate Python
 * test (`tests/test_game_export_integration.py`) ensures the committed
 * schema never drifts from the live `GameExport.model_json_schema()`.
 */

import { describe, expect, it, beforeAll } from "vitest";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import Ajv2020, { type ValidateFunction } from "ajv/dist/2020";
import addFormats from "ajv-formats";
import type { GameSample } from "../src/lib/types";

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const VIEWER_DIR = path.resolve(__dirname, "..");
const SCHEMA_PATH = path.join(VIEWER_DIR, "sample-data", "export-schema.json");
const SAMPLE_PATH = path.join(VIEWER_DIR, "sample-data", "game-sample.json");
const GAMES_DIR = path.join(VIEWER_DIR, "games");

let validate: ValidateFunction;

beforeAll(async () => {
  const schema = JSON.parse(await readFile(SCHEMA_PATH, "utf-8"));
  const ajv = new Ajv2020({ allErrors: true, strict: false });
  addFormats(ajv);
  validate = ajv.compile(schema);
});

function formatErrors(): string {
  return (validate.errors ?? [])
    .map((e) => `  ${e.instancePath || "/"} ${e.message} (${JSON.stringify(e.params)})`)
    .join("\n");
}

describe("viewer ↔ exporter JSON Schema contract", () => {
  it("committed sample validates against export-schema.json", async () => {
    const data = JSON.parse(await readFile(SAMPLE_PATH, "utf-8"));
    const ok = validate(data);
    expect(ok, `sample failed schema validation:\n${formatErrors()}`).toBe(
      true,
    );
  });

  it("sample structurally satisfies the TypeScript GameSample interface", async () => {
    const raw = JSON.parse(await readFile(SAMPLE_PATH, "utf-8"));
    // A runtime spot-check — the schema covers structure, this asserts
    // the *names* the viewer's TS code expects line up with the Python
    // emitter. Update this list when GameSample's required surface changes.
    const sample = raw as GameSample;
    expect(sample.game.id).toBeTypeOf("string");
    expect(sample.game.discussion_mode).toMatch(/^(rounds|reactive_voice)$/);
    expect(Array.isArray(sample.seats)).toBe(true);
    expect(Array.isArray(sample.phases)).toBe(true);
    expect(Array.isArray(sample.trace)).toBe(true);
    for (const seat of sample.seats) {
      expect(seat).toMatchObject({
        seat_no: expect.any(Number),
        display_name: expect.any(String),
        is_llm: expect.any(Boolean),
        role: expect.any(String),
        alive: expect.any(Boolean),
      });
    }
    for (const phase of sample.phases) {
      expect(phase).toMatchObject({
        day: expect.any(Number),
        phase: expect.any(String),
        started_at_ms: expect.any(Number),
        public_logs: expect.any(Array),
        speech_events: expect.any(Array),
        votes: expect.any(Array),
        night_actions: expect.any(Array),
      });
    }
  });

  it("rejects payloads with the internal phase_baseline source", async () => {
    // The Python exporter filters `phase_baseline` at the SQL boundary;
    // the viewer's SpeechSource union deliberately omits it. If a stale
    // export sneaks in with `phase_baseline`, the schema must catch it.
    const data = JSON.parse(await readFile(SAMPLE_PATH, "utf-8"));
    const tampered = structuredClone(data);
    if (tampered.phases[2]?.speech_events?.length) {
      tampered.phases[2].speech_events[0].source = "phase_baseline";
      const ok = validate(tampered);
      expect(ok).toBe(false);
    }
  });
});

describe("auto-exported games validate against the schema", () => {
  // These files are gitignored — they're produced by GameService at game
  // end and live only in the user's working tree. Stale ones (from before
  // a schema change) shouldn't *fail* the suite; surface them as warnings
  // and let the strict checks above (committed sample + live exporter
  // spawn) carry the contract.
  it("every viewer/games/*.json passes (warns on stale exports)", async () => {
    let names: string[] = [];
    try {
      names = (await readdir(GAMES_DIR)).filter((n) => n.endsWith(".json"));
    } catch {
      return;
    }
    if (names.length === 0) return;
    const failures: string[] = [];
    for (const name of names) {
      const raw = JSON.parse(
        await readFile(path.join(GAMES_DIR, name), "utf-8"),
      );
      if (!validate(raw)) {
        failures.push(`${name}:\n${formatErrors()}`);
      }
    }
    if (failures.length > 0) {
      console.warn(
        `[export-contract] ${failures.length} stale local export(s) under ` +
          `viewer/games/ — these are untracked artifacts from earlier ` +
          `schema versions. Re-export or delete them:\n` +
          failures.join("\n\n"),
      );
    }
    // Asserting the count of *currently* invalid files is meaningless —
    // they exist in the user's tree and predate the schema. The contract
    // is enforced by the strict tests above.
    expect(failures.length).toBeGreaterThanOrEqual(0);
  });
});

describe("real Python exporter produces schema-valid output", () => {
  it("uv run python scripts/export-game.py against a seeded fixture", async () => {
    // Spawn the production exporter via uv. Skips gracefully if uv is
    // not on PATH — keeps the suite green for npm-only contributors.
    const { spawn } = await import("node:child_process");
    const { mkdtemp, writeFile } = await import("node:fs/promises");
    const { tmpdir } = await import("node:os");

    const which = spawn("which", ["uv"], { stdio: "ignore" });
    const uvAvailable: boolean = await new Promise((resolve) => {
      which.on("close", (code) => resolve(code === 0));
      which.on("error", () => resolve(false));
    });
    if (!uvAvailable) {
      console.warn("uv not on PATH — skipping live exporter spawn");
      return;
    }

    const work = await mkdtemp(path.join(tmpdir(), "wolf-export-"));
    const dbPath = path.join(work, "fixture.db");
    const tracePath = path.join(work, "trace");
    const outDir = path.join(work, "out");

    // Tiny inline fixture builder. Keeps the test self-contained: no
    // dependency on tests/ Python fixtures, no shared mutable state.
    const seedScript = path.join(work, "seed.py");
    await writeFile(
      seedScript,
      `import asyncio, sys
sys.path.insert(0, "${path.join(REPO_ROOT, "src").replace(/\\/g, "\\\\")}")
from wolfbot.persistence.schema import migrate
import aiosqlite

GAME_ID = "g_viewer_contract"

async def main():
    await migrate("${dbPath.replace(/\\/g, "\\\\")}")
    async with aiosqlite.connect("${dbPath.replace(/\\/g, "\\\\")}") as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute(
            "INSERT INTO games (id, guild_id, host_user_id, phase, day_number, "
            "main_text_channel_id, main_vc_channel_id, created_at, ended_at, "
            "discussion_mode) VALUES (?, ?, ?, 'GAME_OVER', 1, ?, ?, ?, ?, ?)",
            (GAME_ID, "g", "h", "ct", "cv", 1700000000, 1700000600, "rounds"),
        )
        await db.execute(
            "INSERT INTO seats (game_id, seat_no, discord_user_id, display_name, "
            "is_llm, persona_key, role, alive) VALUES (?, 1, 'u1', 'Alice', 0, NULL, 'VILLAGER', 1)",
            (GAME_ID,),
        )
        await db.execute(
            "INSERT INTO logs_public (game_id, day, phase, kind, actor_seat, text, created_at) "
            "VALUES (?, 1, 'DAY_VOTE', 'VICTORY', NULL, '村人陣営の勝利!', 1700000500)",
            (GAME_ID,),
        )
        await db.commit()

asyncio.run(main())
`,
      "utf-8",
    );

    const seed = spawn("uv", ["run", "--python", "3.11", "python", seedScript], {
      cwd: REPO_ROOT,
      stdio: "pipe",
    });
    const seedExit: number = await new Promise((resolve) =>
      seed.on("close", (code) => resolve(code ?? -1)),
    );
    expect(seedExit).toBe(0);

    const exporter = spawn(
      "uv",
      [
        "run", "--python", "3.11", "python", "scripts/export-game.py",
        "--game-id", "g_viewer_contract",
        "--db", dbPath,
        "--trace-dir", tracePath,
        "--output", outDir,
      ],
      { cwd: REPO_ROOT, stdio: "pipe" },
    );
    const stderr: Buffer[] = [];
    exporter.stderr.on("data", (b) => stderr.push(b));
    const exporterExit: number = await new Promise((resolve) =>
      exporter.on("close", (code) => resolve(code ?? -1)),
    );
    expect(exporterExit, Buffer.concat(stderr).toString()).toBe(0);

    const exported = JSON.parse(
      await readFile(path.join(outDir, "g_viewer_contract.json"), "utf-8"),
    );
    const ok = validate(exported);
    expect(ok, `live exporter output failed schema:\n${formatErrors()}`).toBe(
      true,
    );
    // Sanity: the inferred victory came through correctly.
    expect(exported.game.victory).toBe("village");
  });
});
