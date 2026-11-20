// Browser-enforced CORS in real Chromium (Node fetch does not enforce CORS; these do).
//
// IMPORTANT: these tests use browser contexts WITHOUT request interception. While Playwright
// routes are active, Playwright answers CORS preflights itself, so the browser's real preflight
// never reaches the server. Both origins are therefore real local servers:
//   allowed:    http://127.0.0.1:5173 (the Vite dev server; a plain document, /robots.txt)
//   disallowed: http://127.0.0.1:5199 (a tiny local server started here; not on the allow-list)
import { createServer, type Server } from "node:http";
import type { Browser, BrowserContext, Page } from "@playwright/test";
import { expect, test } from "./fixtures";

const APP_ORIGIN = "http://127.0.0.1:5173";
const EVIL_ORIGIN = "http://127.0.0.1:5199";

type Outcome = { ok: true; status: number; body: unknown } | { ok: false; error: string };

/** fetch() run inside the page, so Chromium applies CORS for that page's origin. */
function browserFetch(page: Page, url: string, init: RequestInit): Promise<Outcome> {
  return page.evaluate(
    async ([u, i]) => {
      try {
        const res = await fetch(u, i);
        const text = await res.text();
        return { ok: true as const, status: res.status, body: text ? JSON.parse(text) : null };
      } catch (e) {
        return { ok: false as const, error: String(e) };
      }
    },
    [url, init] as const,
  );
}

let evil: Server;
test.beforeAll(async () => {
  evil = createServer((_req, res) => {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end("<!doctype html><title>another origin</title>");
  });
  await new Promise<void>((r) => evil.listen(5199, "127.0.0.1", r));
});
test.afterAll(() => new Promise<void>((r) => evil.close(() => r())));

/** A context with no interception; every request it makes must stay on 127.0.0.1. */
async function plainContext(browser: Browser): Promise<{ ctx: BrowserContext; urls: string[] }> {
  const ctx = await browser.newContext();
  const urls: string[] = [];
  ctx.on("request", (r) => urls.push(r.url()));
  return { ctx, urls };
}
const local = (urls: string[]) =>
  expect(urls.filter((u) => new URL(u).hostname !== "127.0.0.1")).toEqual([]);

const auth = { Authorization: "Bearer harness-token-user" };
const json = { ...auth, "Content-Type": "application/json" };

test("allowed origin: PATCH and DELETE preflights pass; success and error responses are readable", async ({
  browser,
  harness,
}) => {
  await harness.reset(["fixed_jan"]);
  const { ctx, urls } = await plainContext(browser);
  const page = await ctx.newPage();
  await page.goto(`${APP_ORIGIN}/robots.txt`);
  const api = harness.apiUrl;
  const preflights = await harness.count("OPTIONS");

  const headers: Record<string, string | undefined>[] = [];
  page.on("response", (r) => {
    if (r.url().startsWith(api)) headers.push(r.headers());
  });

  // PATCH with Authorization + JSON: preflighted, then a readable 409 (stale version).
  const patch = await browserFetch(page, `${api}/subscriptions/TPE-OSA_2027-01`, {
    method: "PATCH",
    headers: json,
    body: JSON.stringify({ target_price: 1, if_version: "staleVersion0000000000" }),
  });
  expect(patch).toMatchObject({ ok: true, status: 409, body: { error: "conflict" } });
  // DELETE: preflighted, then a readable 404 for an unknown ID.
  const del = await browserFetch(
    page,
    `${api}/subscriptions/TPE-XXX_2027-01?if_version=${"Z".repeat(22)}`,
    {
      method: "DELETE",
      headers: auth,
    },
  );
  expect(del).toMatchObject({ ok: true, status: 404, body: { error: "not_found" } });
  // A gateway error is readable too (CORS headers on gateway responses).
  await harness.fault(503);
  const fault = await browserFetch(page, `${api}/subscriptions`, { headers: auth });
  expect(fault).toMatchObject({ ok: true, status: 503 });
  // A missing token: a readable 401.
  const noAuth = await browserFetch(page, `${api}/subscriptions`, {});
  expect(noAuth).toMatchObject({ ok: true, status: 401, body: { error: "unauthorized" } });

  expect(await harness.count("OPTIONS")).toBeGreaterThanOrEqual(preflights + 2);
  expect(await harness.count("PATCH /subscriptions/{id}")).toBe(1);
  expect(await harness.count("DELETE /subscriptions/{id}")).toBe(1);
  for (const h of headers) {
    expect(h["access-control-allow-origin"]).toBe(APP_ORIGIN); // never "*"
    expect(h["access-control-allow-credentials"]).toBeUndefined();
  }
  expect(headers.length).toBeGreaterThanOrEqual(4);
  expect((await harness.rows())[0]!.target_price).toBe(8000); // the stale PATCH changed nothing
  local(urls);
  await ctx.close();
});

test("disallowed origin: Chromium blocks preflighted writes and hides responses", async ({
  browser,
  harness,
}) => {
  await harness.reset(["fixed_jan"]);
  const before = await harness.rows();
  const { ctx, urls } = await plainContext(browser);
  const page = await ctx.newPage();
  await page.goto(`${EVIL_ORIGIN}/`);
  const api = harness.apiUrl;
  const counts = async () => (await harness.state()).requests;

  // Preflighted PATCH and DELETE: the preflight gets no CORS headers, so Chromium never sends them.
  const start = await counts();
  for (const [method, url, body] of [
    [
      "PATCH",
      `${api}/subscriptions/TPE-OSA_2027-01`,
      JSON.stringify({ target_price: 1, if_version: before[0]!.version_token }),
    ],
    [
      "DELETE",
      `${api}/subscriptions/TPE-OSA_2027-01?if_version=${before[0]!.version_token}`,
      undefined,
    ],
  ] as const) {
    const out = await browserFetch(page, url, {
      method,
      headers: json,
      ...(body ? { body } : {}),
    });
    expect(out.ok, method).toBe(false);
    expect((out as { error: string }).error).toMatch(/Failed to fetch|TypeError/);
  }
  const afterPreflights = await counts();
  expect((afterPreflights["OPTIONS"] ?? 0) - (start["OPTIONS"] ?? 0)).toBe(2); // only preflights arrived
  expect(afterPreflights["PATCH /subscriptions/{id}"] ?? 0).toBe(0);
  expect(afterPreflights["DELETE /subscriptions/{id}"] ?? 0).toBe(0);

  // A simple GET (no preflight) does reach the server, but Chromium blocks the response.
  const simple = await browserFetch(page, `${api}/subscriptions`, {});
  expect(simple.ok).toBe(false);
  expect((await counts())["GET /subscriptions"] ?? 0).toBe(1);

  expect(await harness.rows()).toEqual(before); // nothing changed
  local(urls);
  await ctx.close();
});
