import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
    environment: "node",
    // The contract test executes the Python exporter as a subprocess —
    // give it room to finish on a cold cache.
    testTimeout: 60_000,
  },
});
