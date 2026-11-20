// Phase 3B-D browser end-to-end tests (Chromium). See docs/m1.4-phase3b-e2e.md.
// Deterministic by design: one worker, no retries (a flaky test fails instead of being hidden),
// fixed clock and fixed test data per test. Traces and screenshots only on failure; they contain
// only fake test tokens and @example.com data.
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testIgnore: ["isolation.spec.ts"],
  globalSetup: "./e2e/global-setup.ts",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  forbidOnly: true,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  outputDir: "test-results/e2e",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    serviceWorkers: "block",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
