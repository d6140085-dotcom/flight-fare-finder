import { describe, expect, it } from "vitest";

import { m14PrototypeEnabled } from "./prototype-flag";

describe("M1.4 prototype build flag (fail-closed)", () => {
  it.each([
    ["serve", undefined, true], // local `vite` dev server
    ["serve", "development", true], // `vercel dev`
    ["build", "preview", true], // Vercel preview deployment
    ["build", "production", false],
    ["serve", "production", false], // production never, even on a dev server
    ["build", undefined, false], // unknown: local `vite build`, other hosts
    ["build", "development", false],
    ["build", "staging", false], // custom Vercel environments
    ["build", "", false],
    ["build", "Preview", false], // exact match only
  ] as const)("command=%s VERCEL_ENV=%s → %s", (command, vercelEnv, expected) => {
    expect(m14PrototypeEnabled({ command, vercelEnv })).toBe(expected);
  });
});
