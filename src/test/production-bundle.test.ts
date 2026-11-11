// @vitest-environment node
// Builds the real bundle the way Vercel does and scans it: the production build must contain no
// Phase 3A code, routes, mock data or test marker, and must still contain the M1 app. A preview
// build is scanned too, to prove the scan would notice the code if it were there.
import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { build } from "vite";

const root = path.resolve(__dirname, "../..");
const dirs: string[] = [];

async function bundleText(vercelEnv: "production" | "preview"): Promise<string> {
  const outDir = mkdtempSync(path.join(tmpdir(), `m14-${vercelEnv}-`));
  dirs.push(outDir);
  const before = process.env["VERCEL_ENV"];
  process.env["VERCEL_ENV"] = vercelEnv;
  try {
    await build({
      root,
      configFile: path.join(root, "vite.config.ts"),
      logLevel: "silent",
      build: { outDir, emptyOutDir: true },
    });
  } finally {
    if (before === undefined) delete process.env["VERCEL_ENV"];
    else process.env["VERCEL_ENV"] = before;
  }
  const assets = path.join(outDir, "assets");
  return readdirSync(assets)
    .filter((f) => f.endsWith(".js"))
    .map((f) => readFileSync(path.join(assets, f), "utf8"))
    .join("\n");
}

const PHASE_3A_MARKERS = [
  "app/subscriptions",
  "prototype/subscriptions",
  "marker=000000",
  "模擬情境",
  "開發預覽",
  "Mock subscription data",
  "試用自訂航線訂閱",
];

afterAll(() => {
  for (const d of dirs) rmSync(d, { recursive: true, force: true });
});

describe("built bundles", () => {
  it("production: no Phase 3A routes, mock client, scenarios or test marker; M1 intact", async () => {
    const js = await bundleText("production");
    for (const s of PHASE_3A_MARKERS) expect(js, s).not.toContain(s);
    // The M1 dashboard is still there.
    expect(js).toContain("Pick a route and a target price");
    expect(js).toContain("/subscriptions"); // M1 GET /subscriptions API path
  }, 120_000);

  it("preview: the same scan finds the Phase 3A code (control)", async () => {
    const js = await bundleText("preview");
    for (const s of ["app/subscriptions", "模擬情境", "marker=000000"]) expect(js, s).toContain(s);
  }, 120_000);
});
