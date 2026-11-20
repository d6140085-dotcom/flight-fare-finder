// Phase 3B-D isolation suite: builds production and Vercel-preview bundles with EVERY local
// HTTP-mode variable set (as if someone configured them on Vercel) and serves each one with
// `vite preview` on 127.0.0.1. e2e/isolation.spec.ts then checks them in Chromium.
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const ROOT = join(import.meta.dirname, "..");
const VITE = join(ROOT, "node_modules/.bin/vite");
export const TARGETS = { production: 4180, preview: 4181 } as const;
export const HOSTILE = {
  VITE_M14_HTTP_ALLOWED: "true",
  VITE_M14_API_MODE: "http",
  VITE_M14_API_URL: "http://127.0.0.1:8787",
  VITE_M14_DEV_TOKEN: "harness-token-user",
};

function env(extra: Record<string, string>): Record<string, string> {
  return {
    PATH: process.env["PATH"] ?? "/usr/bin:/bin",
    HOME: process.env["HOME"] ?? tmpdir(),
    VITE_SUPABASE_URL: "https://e2e-fake-project.supabase.invalid",
    VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_e2e_placeholder",
    VITE_FLIGHT_API_URL: "http://127.0.0.1:9",
    ...HOSTILE,
    ...extra,
  };
}

async function waitFor(url: string) {
  for (let i = 0; i < 240; i++) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`timed out waiting for ${url}`);
}

export default async function isolationSetup(): Promise<() => Promise<void>> {
  const servers: ChildProcess[] = [];
  for (const [vercelEnv, port] of Object.entries(TARGETS)) {
    const outDir = mkdtempSync(join(tmpdir(), `ffe-${vercelEnv}-`));
    const build = spawnSync(VITE, ["build", "--outDir", outDir, "--emptyOutDir"], {
      cwd: ROOT,
      env: env({ VERCEL_ENV: vercelEnv }),
      encoding: "utf8",
    });
    if (build.status !== 0) throw new Error(`${vercelEnv} build failed:\n${build.stderr}`);
    const server = spawn(
      VITE,
      [
        "preview",
        "--outDir",
        outDir,
        "--host",
        "127.0.0.1",
        "--port",
        String(port),
        "--strictPort",
      ],
      { cwd: ROOT, env: env({ VERCEL_ENV: vercelEnv }), stdio: "ignore" },
    );
    servers.push(server);
    await waitFor(`http://127.0.0.1:${port}/`);
    console.log(`[isolation] ${vercelEnv} build served on 127.0.0.1:${port}`);
  }
  return async () => {
    for (const s of servers) s.kill("SIGTERM");
  };
}
