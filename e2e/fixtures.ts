// Phase 3B-D fixtures: harness control, a browser-side fake Supabase, a fixed clock and a
// network guard that blocks every non-local request.
//
// Fake authentication: the app's own Supabase client and route guard run unchanged. A fake
// session is put in localStorage and Chromium's requests to the fake Supabase host are answered
// here. This exercises the app's sign-in handling only; it says nothing about production
// Supabase authentication (the harness API is called with the local dev token, never this
// session).
import { readFileSync } from "node:fs";
import {
  test as base,
  expect,
  type BrowserContext,
  type Page,
  type Request,
} from "@playwright/test";
import vectors from "../aws/tests/vectors/subscriptions-http.json" with { type: "json" };
import { APP_ORIGIN, FAKE_SUPABASE_URL, STATE_FILE } from "./global-setup";

export { expect };
export const NOW = new Date(vectors.now); // 2026-11-05T03:00:00Z: selectable 2026-12 … 2027-11
export const USER = "user@example.com";
export const ROWS = vectors.rows as unknown as Record<string, Record<string, unknown>>;
const FAKE_HOST = new URL(FAKE_SUPABASE_URL).hostname;
export const STORAGE_KEY = `sb-${FAKE_HOST.split(".")[0]}-auth-token`;
/** Hosts the page may try to reach that are blocked without failing a test (web fonts). */
const TOLERATED = new Set(["fonts.googleapis.com", "fonts.gstatic.com"]);

export interface HarnessState {
  rows: Record<string, Row[]>;
  quota: Record<string, { count: number; version?: number }>;
  requests: Record<string, number>;
  /** Every request the function handled: its route key, status and the DynamoDB operations it made. */
  handler_calls: { route: string; status: number | null; ddb: string[] }[];
  /** Requests that reached the harness's fake Supabase (the backend's session check). */
  supabase_requests: number;
}
/** Faults the harness's fake Supabase can answer the backend's session check with. */
export type SupabaseFault =
  400 | 401 | 403 | 404 | 408 | 429 | 500 | 502 | 503 | 504 | "timeout" | "reset" | "invalid_json";
export interface Row {
  email: string;
  route: string;
  origin: string;
  destination: string;
  departure_month?: string | null;
  target_price: number;
  alerts_paused?: boolean;
  version_token?: string;
  updated_at?: string;
}

const info = () =>
  JSON.parse(readFileSync(process.env["E2E_STATE_FILE"] ?? STATE_FILE, "utf8")) as {
    api_url: string;
    control_url: string;
    control_token: string;
    engine: string;
  };

export class Harness {
  readonly apiUrl: string;
  readonly engine: string;
  private readonly control: string;
  private readonly secret: string;
  constructor() {
    const i = info();
    this.apiUrl = i.api_url;
    this.engine = i.engine;
    this.control = i.control_url;
    this.secret = i.control_token;
  }
  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(`${this.control}${path}`, {
      method,
      headers: { "X-Harness-Control": this.secret, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (res.status !== 200) throw new Error(`harness ${path}: ${res.status} ${await res.text()}`);
    return (await res.json()) as T;
  }
  /** Fresh empty tables, the fixed clock, optional seed rows (names from the shared vectors). */
  reset(seed: string[] = [], extra: Record<string, unknown>[] = []) {
    return this.call("POST", "/reset", {
      now: vectors.now,
      rows: { [USER]: [...seed.map((n) => ROWS[n]), ...extra] },
    });
  }
  state() {
    return this.call<HarnessState>("GET", "/state");
  }
  fault(status: 429 | 502 | 503 | 504) {
    return this.call("POST", "/fault", { status });
  }
  /** The backend's next `count` session checks get this answer from Supabase. */
  supabaseFault(kind: SupabaseFault, count = 1) {
    return this.call("POST", "/supabase-fault", { kind, count });
  }
  async rows(): Promise<Row[]> {
    return (await this.state()).rows[USER] ?? [];
  }
  async count(key: string): Promise<number> {
    return (await this.state()).requests[key] ?? 0;
  }
}

function b64url(o: unknown): string {
  return Buffer.from(JSON.stringify(o)).toString("base64url");
}
/** A JWT-shaped fake token (unsigned: it never leaves the browser except to the fake host). */
function fakeJwt(kind: string, exp: number): string {
  return `${b64url({ alg: "HS256", typ: "JWT" })}.${b64url({
    sub: "00000000-0000-4000-8000-00000000e2e0",
    email: USER,
    role: "authenticated",
    aud: "authenticated",
    exp,
    e2e: kind,
  })}.e2e-fake-signature`;
}

export type SessionKind = "valid" | "expired" | "revoked";
const nowSec = Math.floor(NOW.getTime() / 1000);
export const FAKE_TOKENS: Record<SessionKind, string> = {
  valid: fakeJwt("valid", nowSec + 3600),
  expired: fakeJwt("expired", nowSec - 3600),
  revoked: fakeJwt("revoked", nowSec + 3600),
};
const FAKE_USER = {
  id: "00000000-0000-4000-8000-00000000e2e0",
  aud: "authenticated",
  role: "authenticated",
  email: USER,
  app_metadata: { provider: "email" },
  user_metadata: {},
  created_at: "2026-10-01T00:00:00Z",
};

/** Puts a fake session in localStorage before the app's code runs. */
export async function signIn(context: BrowserContext, kind: SessionKind = "valid") {
  const exp = kind === "expired" ? nowSec - 3600 : nowSec + 3600;
  const session = {
    access_token: FAKE_TOKENS[kind],
    token_type: "bearer",
    expires_in: 3600,
    expires_at: exp,
    refresh_token: `e2e-refresh-${kind}`,
    user: FAKE_USER,
  };
  await context.addInitScript(([key, value]) => window.localStorage.setItem(key, value), [
    STORAGE_KEY,
    JSON.stringify(session),
  ] as const);
}

export interface Net {
  /** Requests to the harness API, in order: "METHOD /path" and the Authorization header. */
  api: { method: string; path: string; auth: string | null; page: Page }[];
  /** Access tokens the fake Supabase saw on /auth/v1/user. */
  supabaseUserCalls: string[];
  blocked: string[];
}

export const test = base.extend<{ harness: Harness; net: Net }>({
  harness: [
    async ({ browserName: _browserName }, provide) => {
      const h = new Harness();
      await h.reset(); // every test starts from empty test tables
      await provide(h);
    },
    { auto: true },
  ],
  net: [
    async ({ context, harness }, provide) => {
      const net: Net = { api: [], supabaseUserCalls: [], blocked: [] };
      const apiOrigin = new URL(harness.apiUrl).origin;
      await context.route("**/*", async (route) => {
        const url = new URL(route.request().url());
        if (url.hostname === FAKE_HOST) return fakeSupabase(route.request(), net, route);
        if (url.hostname === "127.0.0.1" || url.hostname === "localhost") return route.fallback();
        net.blocked.push(url.hostname); // every other host: blocked, never contacted
        return route.abort("blockedbyclient");
      });
      context.on("request", (req: Request) => {
        if (!req.url().startsWith(apiOrigin) || req.method() === "OPTIONS") return;
        const u = new URL(req.url());
        net.api.push({
          method: req.method(),
          path: u.pathname + u.search,
          auth: req.headers()["authorization"] ?? null,
          page: req.frame().page(),
        });
      });
      await provide(net);
      const unexpected = net.blocked.filter((h) => !TOLERATED.has(h));
      expect(unexpected, "unexpected non-local requests").toEqual([]);
      for (const call of net.api) {
        expect(call.auth).toBe("Bearer harness-token-user"); // never a Supabase token
      }
    },
    { auto: true },
  ],
  page: async ({ page }, provide) => {
    await page.clock.setFixedTime(NOW);
    await provide(page);
  },
});

async function fakeSupabase(
  req: Request,
  net: Net,
  route: Parameters<Parameters<BrowserContext["route"]>[1]>[0],
) {
  const url = new URL(req.url());
  const json = (status: number, body: unknown) =>
    route.fulfill({
      status,
      contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": APP_ORIGIN, "Access-Control-Allow-Headers": "*" },
      body: JSON.stringify(body),
    });
  if (req.method() === "OPTIONS") {
    return route.fulfill({
      status: 204,
      headers: {
        "Access-Control-Allow-Origin": APP_ORIGIN,
        "Access-Control-Allow-Headers":
          "authorization,apikey,content-type,x-client-info,x-supabase-api-version",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
      },
    });
  }
  if (url.pathname === "/auth/v1/user" && req.method() === "GET") {
    const token = (req.headers()["authorization"] ?? "").replace(/^Bearer /, "");
    net.supabaseUserCalls.push(token);
    if (token === FAKE_TOKENS.valid) return json(200, FAKE_USER);
    return json(401, { code: 401, error_code: "bad_jwt", msg: "invalid JWT" });
  }
  if (url.pathname === "/auth/v1/token") {
    return json(400, { error: "invalid_grant", error_description: "Invalid Refresh Token" });
  }
  if (url.pathname === "/auth/v1/logout") return route.fulfill({ status: 204 });
  return json(404, { msg: "not found" });
}

// ---- page helpers --------------------------------------------------------------------------

export const APP = "/app/subscriptions";
export const list = (page: Page) => page.getByRole("list", { name: "訂閱列表" });
export const card = (page: Page, name: string) =>
  list(page).getByRole("listitem", { name, exact: true });
export const createForm = (page: Page) => page.getByRole("form", { name: "新增訂閱" });
export const monthLabel = (m: string) => `${Number(m.slice(0, 4))} 年 ${Number(m.slice(5, 7))} 月`;

export async function openApp(page: Page, used: number) {
  await page.goto(APP);
  await expect(page.getByText("本機開發（本機 HTTP 測試環境）")).toBeVisible();
  await expect(page.getByText(`我的航線訂閱：${used} / 10`)).toBeVisible();
}

export async function fillCreate(
  page: Page,
  o: { origin?: string; dest: string; month: string; target: string },
) {
  const form = createForm(page);
  await form.getByLabel("出發地").selectOption(o.origin ?? "TPE");
  const box = form.getByRole("combobox", { name: "目的地" });
  await box.fill(o.dest);
  await box.press("Enter");
  await form.getByLabel("出發月份").selectOption(o.month);
  await form.getByLabel("目標價（新台幣）").fill(o.target);
}

/** The API calls one page made, optionally filtered ("GET", "PATCH"...). */
export const calls = (net: Net, page: Page, method?: string) =>
  net.api.filter((c) => c.page === page && (!method || c.method === method));
