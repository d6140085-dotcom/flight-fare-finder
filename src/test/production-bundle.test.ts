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

async function bundleText(
  vercelEnv: "production" | "preview",
  extraEnv: Record<string, string> = {},
  define: Record<string, string> = {},
): Promise<string> {
  const outDir = mkdtempSync(path.join(tmpdir(), `m14-${vercelEnv}-`));
  dirs.push(outDir);
  const saved = Object.fromEntries(
    ["VERCEL_ENV", ...Object.keys(extraEnv)].map((k) => [k, process.env[k]]),
  );
  Object.assign(process.env, { VERCEL_ENV: vercelEnv, ...extraEnv });
  try {
    await build({
      root,
      configFile: path.join(root, "vite.config.ts"),
      logLevel: "silent",
      build: { outDir, emptyOutDir: true },
      define,
    });
  } finally {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
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
    // B6 MVP safety: the M1 plan cards no longer claim a recent fare (hard-coded amounts removed).
    for (const s of ["近期最低價", "Recent lowest fare", "hintTwd"]) expect(js, s).not.toContain(s);
    expect(js).toContain("設定你的目標價（新台幣）");
  }, 120_000);

  it("preview: the same scan finds the Phase 3A code (control)", async () => {
    const js = await bundleText("preview");
    for (const s of ["app/subscriptions", "模擬情境", "marker=000000"]) expect(js, s).toContain(s);
  }, 120_000);

  // Phase 3B-C: even with every local-HTTP-mode variable set (as if someone configured them on
  // Vercel), neither a production nor a preview build contains the HTTP client, the harness URL,
  // a development token or the development authentication override.
  const HOSTILE = {
    VITE_M14_HTTP_ALLOWED: "true",
    VITE_M14_API_MODE: "http",
    VITE_M14_API_URL: "http://127.0.0.1:8787",
    VITE_M14_DEV_TOKEN: "harness-token-user",
  };
  const HTTP_MARKERS = [
    "harness-token", // development tokens (and the allow-list)
    "127.0.0.1:8787", // the harness URL
    "HttpSubscriptionsClient: baseUrl", // the HTTP client's own messages
    "Unknown VITE_M14_API_MODE", // the HTTP branch of the factory
    "localhost harness", // the HTTP branch of the factory
    "本機 HTTP 測試環境", // the HTTP-mode page banner
  ];

  it("control: forcing the dev-server switch on puts every marker in the bundle", async () => {
    const forced = Object.fromEntries(
      Object.entries(HOSTILE).map(([k, v]) => [`import.meta.env.${k}`, JSON.stringify(v)]),
    );
    const js = await bundleText("preview", HOSTILE, forced);
    for (const s of HTTP_MARKERS) expect(js, s).toContain(s);
  }, 120_000);

  it.each(["production", "preview"] as const)(
    "%s with every HTTP-mode variable set: no HTTP client, token, harness URL or override",
    async (env) => {
      const js = await bundleText(env, HOSTILE);
      for (const s of HTTP_MARKERS) expect(js, s).not.toContain(s);
      // Vite inlines the env object where code reads it dynamically; there the HTTP-mode
      // settings must be the blank build-time constants and the switch "false".
      expect(js).not.toMatch(/VITE_M14_(?:DEV_TOKEN|API_URL|API_MODE):`[^`]/);
      expect(js).not.toMatch(/VITE_M14_HTTP_ALLOWED:`(?!false`)/);
      for (const m of js.matchAll(
        /VITE_M14_(DEV_TOKEN|API_URL|API_MODE|HTTP_ALLOWED):`([^`]*)`/g,
      )) {
        expect(m[2], m[1]).toBe(m[1] === "HTTP_ALLOWED" ? "false" : "");
      }
      if (env === "preview") expect(js).toContain("模擬情境"); // still the mock page
    },
    120_000,
  );
});
