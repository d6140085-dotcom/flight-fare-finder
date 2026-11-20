// Production and Vercel-preview builds, made with every local HTTP-mode variable set, cannot
// use the HTTP client: production has no M1.4 page at all; preview shows only the mock. The
// browser never contacts the harness URL from the variables and never sends a dev token.
import { expect, test, type BrowserContext } from "@playwright/test";
import { HOSTILE, TARGETS } from "./isolation-setup";

const HARNESS_PORT = new URL(HOSTILE.VITE_M14_API_URL).port;

async function watch(context: BrowserContext, port: number) {
  const seen: { url: string; auth: string | undefined; type: string }[] = [];
  await context.route("**/*", (route) => {
    const u = new URL(route.request().url());
    seen.push({
      url: u.href,
      auth: route.request().headers()["authorization"],
      type: route.request().resourceType(),
    });
    // Only the server under test; everything else (fonts, the fake Supabase, the harness URL
    // from the variables) is blocked and recorded.
    return u.hostname === "127.0.0.1" && u.port === String(port)
      ? route.fallback()
      : route.abort("blockedbyclient");
  });
  return seen;
}

function expectIsolated(seen: { url: string; auth: string | undefined; type: string }[]) {
  expect(seen.filter((r) => new URL(r.url).port === HARNESS_PORT)).toEqual([]);
  expect(seen.filter((r) => (r.auth ?? "").includes("harness-token"))).toEqual([]);
  // No API call of any kind (the mock never makes one).
  expect(seen.filter((r) => r.type === "fetch" || r.type === "xhr")).toEqual([]);
}

test("production build: no M1.4 pages, no HTTP mode", async ({ page, context }) => {
  const port = TARGETS.production;
  const seen = await watch(context, port);
  for (const path of ["/prototype/subscriptions", "/app/subscriptions"]) {
    await page.goto(`http://127.0.0.1:${port}${path}`);
    await expect(page.getByRole("heading", { name: "Page not found" })).toBeVisible();
  }
  await expect(page.getByText("本機 HTTP 測試環境")).toHaveCount(0);
  expectIsolated(seen);
});

test("preview build: the mock page only, never HTTP mode", async ({ page, context }) => {
  const port = TARGETS.preview;
  const seen = await watch(context, port);
  await page.goto(`http://127.0.0.1:${port}/prototype/subscriptions`);
  await expect(page.getByText("開發預覽（模擬資料）")).toBeVisible();
  await expect(page.getByText(/我的航線訂閱：\d+ \/ 10/)).toBeVisible();
  await expect(page.getByLabel("模擬情境")).toBeVisible();
  await expect(page.getByText("本機 HTTP 測試環境")).toHaveCount(0);
  await expect(page.getByText("本機 HTTP 模式設定不正確")).toHaveCount(0);
  expectIsolated(seen);
});
