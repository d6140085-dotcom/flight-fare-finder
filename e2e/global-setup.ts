// Phase 3B-D: starts the isolated services for the browser tests and stops them afterwards.
//
//   Chromium -> Vite dev server (127.0.0.1:5173, local HTTP mode) -> HttpSubscriptionsClient
//            -> Phase 3B-B harness (127.0.0.1, random port) -> real Lambda handler
//            -> DynamoDB Local (DDB_ENDPOINT) or moto in-process (local runs without it)
//
// Both processes get a minimal environment: no real credentials, no proxies, Supabase and the
// M1 API pointed at placeholders that never resolve (the browser's requests to the fake Supabase
// host are answered inside Chromium by e2e/fixtures.ts). Nothing here reaches AWS or Supabase.
import { spawn, type ChildProcess } from "node:child_process";
import { writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";

export const ROOT = join(import.meta.dirname, "..");
export const STATE_FILE = join(tmpdir(), "flight-fare-finder-e2e-harness.json");
export const APP_ORIGIN = "http://127.0.0.1:5173";
export const FAKE_SUPABASE_URL = "https://e2e-fake-project.supabase.invalid";

function cleanEnv(extra: Record<string, string> = {}): Record<string, string> {
  const env: Record<string, string> = {
    PATH: process.env["PATH"] ?? "/usr/bin:/bin",
    HOME: process.env["HOME"] ?? tmpdir(),
    LANG: "C.UTF-8",
  };
  if (process.env["DDB_ENDPOINT"]) env["DDB_ENDPOINT"] = process.env["DDB_ENDPOINT"];
  return { ...env, ...extra };
}

async function waitForHttp(url: string, ms: number): Promise<void> {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    try {
      const res = await fetch(url);
      if (res.ok) return;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`timed out waiting for ${url}`);
}

export default async function globalSetup(): Promise<() => Promise<void>> {
  const children: ChildProcess[] = [];
  const harness = spawn("python3", [join(ROOT, "aws/tools/local_api.py"), "serve", "--control"], {
    cwd: ROOT,
    env: cleanEnv(),
    stdio: ["ignore", "pipe", "pipe"],
  });
  children.push(harness);
  let harnessErr = "";
  harness.stderr!.on("data", (d: Buffer) => (harnessErr += d.toString()));
  const line = await new Promise<string>((resolve, reject) => {
    createInterface({ input: harness.stdout! }).once("line", resolve);
    harness.once("exit", (code) => reject(new Error(`harness exited (${code}): ${harnessErr}`)));
  });
  const info = JSON.parse(line) as { api_url: string; engine: string };
  console.log(`[e2e] harness engine: ${info.engine}`);

  const vite = spawn(
    join(ROOT, "node_modules/.bin/vite"),
    ["--host", "127.0.0.1", "--port", "5173", "--strictPort"],
    {
      cwd: ROOT,
      env: cleanEnv({
        VITE_M14_API_MODE: "http",
        VITE_M14_API_URL: info.api_url,
        VITE_M14_DEV_TOKEN: "harness-token-user",
        VITE_SUPABASE_URL: FAKE_SUPABASE_URL,
        VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_e2e_placeholder",
        VITE_FLIGHT_API_URL: "http://127.0.0.1:9",
      }),
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  children.push(vite);
  let viteOut = "";
  vite.stdout!.on("data", (d: Buffer) => (viteOut += d.toString()));
  vite.stderr!.on("data", (d: Buffer) => (viteOut += d.toString()));
  try {
    await waitForHttp(`${APP_ORIGIN}/`, 90_000);
    await waitForHttp(`${APP_ORIGIN}/src/main.tsx`, 90_000); // warm the module graph
  } catch (e) {
    throw new Error(`${(e as Error).message}\n${viteOut}`);
  }
  writeFileSync(STATE_FILE, JSON.stringify(info));
  process.env["E2E_STATE_FILE"] = STATE_FILE;

  return async () => {
    for (const c of children.reverse()) c.kill("SIGTERM");
  };
}
