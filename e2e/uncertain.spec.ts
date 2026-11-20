// Uncertain write outcomes (mandatory): the page says the change may or may not have been saved,
// reloads the list exactly once, keeps the input and never resends the write.
//
// Post-commit response loss uses a synchronisation barrier: Playwright forwards Chromium's own
// request to the backend (route.fetch), confirms in the stored records that the write COMMITTED,
// and only then cuts the connection (route.abort), so the success response never reaches the
// page. A request aborted before the backend is a separate test.
import type { Page, Route } from "@playwright/test";
import {
  calls,
  card,
  createForm,
  expect,
  fillCreate,
  monthLabel,
  openApp,
  signIn,
  test,
  type Harness,
  type Net,
} from "./fixtures";

const UNCERTAIN = "可能已完成，也可能沒有";
const OSA = `台北 → 大阪・${monthLabel("2027-01")}`;

async function ready(page: Page, context: Parameters<typeof signIn>[0], harness: Harness) {
  await harness.reset(["fixed_jan"]);
  await signIn(context);
  await openApp(page, 1);
}

/** Waits for the one reload that follows an uncertain outcome, then checks nothing else ran. */
async function expectOneReload(page: Page, net: Net, act: () => Promise<unknown>) {
  const reload = page.waitForResponse((r) => r.request().method() === "GET");
  await act();
  await reload;
  await expect(page.getByText(UNCERTAIN)).toBeVisible();
  expect(calls(net, page, "GET")).toHaveLength(2); // initial load + exactly one reload
}

for (const status of [503, 504] as const) {
  test(`HTTP ${status} on create: uncertain, one reload, input kept, no resend`, async ({
    page,
    context,
    harness,
    net,
  }) => {
    await ready(page, context, harness);
    await harness.fault(status);
    await fillCreate(page, { dest: "LON", month: "2027-03", target: "12000" });
    await expectOneReload(page, net, () =>
      createForm(page).getByRole("button", { name: "開始追蹤" }).click(),
    );
    expect(calls(net, page, "POST")).toHaveLength(1);
    expect(await harness.count("POST /subscriptions")).toBe(1); // answered by the gateway fault
    expect((await harness.rows()).map((r) => r.route)).toEqual(["TPE-OSA#2027-01"]);
    await expect(createForm(page).getByLabel("目標價（新台幣）")).toHaveValue("12000");
    await expect(createForm(page).getByLabel("出發月份")).toHaveValue("2027-03");
  });
}

test("network interruption before the backend (DELETE): uncertain, one reload, row intact", async ({
  page,
  context,
  harness,
  net,
}) => {
  await ready(page, context, harness);
  await page.route(
    `${harness.apiUrl}/subscriptions/*`,
    (route: Route) =>
      route.request().method() === "DELETE" ? route.abort("connectionreset") : route.fallback(),
    { times: 1 },
  );
  await card(page, OSA).getByRole("button", { name: "刪除" }).click();
  await expectOneReload(page, net, () => page.getByRole("button", { name: "確認刪除" }).click());
  expect(calls(net, page, "DELETE")).toHaveLength(1);
  expect(await harness.count("DELETE /subscriptions/{id}")).toBe(0); // never reached the backend
  expect((await harness.rows()).map((r) => r.route)).toEqual(["TPE-OSA#2027-01"]);
  await expect(card(page, OSA)).toBeVisible();
});

test("response lost AFTER a committed PATCH: uncertain, one reload shows the saved change, no replay", async ({
  page,
  context,
  harness,
  net,
}) => {
  await ready(page, context, harness);
  const before = (await harness.rows())[0]!;
  let committed: { status: number; price: number; version: string } | null = null;
  await page.route(
    `${harness.apiUrl}/subscriptions/*`,
    async (route: Route) => {
      if (route.request().method() !== "PATCH") return route.fallback();
      const response = await route.fetch(); // Chromium's request reaches the backend once
      const row = (await harness.rows())[0]!; // barrier: the write has committed ...
      committed = {
        status: response.status(),
        price: row.target_price,
        version: row.version_token!,
      };
      expect(committed.status).toBe(200);
      expect(committed.price).toBe(7777);
      expect(committed.version).not.toBe(before.version_token);
      await route.abort("connectionreset"); // ... and only then the response is lost
    },
    { times: 1 },
  );
  await card(page, OSA).getByRole("button", { name: "編輯" }).click();
  const edit = card(page, OSA).getByRole("form", { name: "編輯訂閱" });
  await edit.getByLabel("目標價（新台幣）").fill("7777");
  await expectOneReload(page, net, () => edit.getByRole("button", { name: "儲存變更" }).click());

  expect(committed).not.toBeNull();
  expect(calls(net, page, "PATCH")).toHaveLength(1);
  expect(await harness.count("PATCH /subscriptions/{id}")).toBe(1); // not replayed
  const stored = (await harness.rows())[0]!;
  expect(stored.target_price).toBe(7777); // the change WAS saved
  expect(stored.version_token).toBe(committed!.version);
  // The edit form keeps the input; the user sees the reloaded state and decides.
  await expect(edit.getByLabel("目標價（新台幣）")).toHaveValue("7777");
});

test("response lost AFTER a committed create: the reload shows it; the form keeps the input", async ({
  page,
  context,
  harness,
  net,
}) => {
  await ready(page, context, harness);
  await page.route(
    `${harness.apiUrl}/subscriptions`,
    async (route: Route) => {
      if (route.request().method() !== "POST") return route.fallback();
      const response = await route.fetch();
      expect(response.status()).toBe(201);
      expect((await harness.rows()).map((r) => r.route)).toContain("TPE-LON#2027-03"); // barrier
      await route.abort("connectionreset");
    },
    { times: 1 },
  );
  await fillCreate(page, { dest: "LON", month: "2027-03", target: "12000" });
  await expectOneReload(page, net, () =>
    createForm(page).getByRole("button", { name: "開始追蹤" }).click(),
  );
  await expect(card(page, `台北 → 倫敦・${monthLabel("2027-03")}`)).toBeVisible();
  await expect(page.getByText("我的航線訂閱：2 / 10")).toBeVisible();
  expect(calls(net, page, "POST")).toHaveLength(1);
  expect(await harness.count("POST /subscriptions")).toBe(1);
  expect((await harness.rows()).filter((r) => r.route === "TPE-LON#2027-03")).toHaveLength(1);
  await expect(createForm(page).getByLabel("目標價（新台幣）")).toHaveValue("12000");
});

test("HTTP 429: a rate-limit message, no reload, no automatic retry", async ({
  page,
  context,
  harness,
  net,
}) => {
  await ready(page, context, harness);
  const before = await harness.rows();
  await harness.fault(429);
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.request().method() === "PATCH"),
    card(page, OSA).getByRole("button", { name: "暫停追蹤" }).click(),
  ]);
  expect(res.status()).toBe(429);
  await expect(page.getByText("操作太頻繁，請稍候幾秒再試。")).toBeVisible();
  expect(calls(net, page, "PATCH")).toHaveLength(1);
  expect(calls(net, page, "GET")).toHaveLength(1);
  expect(await harness.rows()).toEqual(before); // nothing written
  // A manual retry by the user works (the fault was one-off).
  await card(page, OSA).getByRole("button", { name: "暫停追蹤" }).click();
  await expect(card(page, OSA).getByRole("button", { name: "恢復追蹤" })).toBeVisible();
  expect(calls(net, page, "PATCH")).toHaveLength(2);
});

test("double clicks submit once", async ({ page, context, harness, net }) => {
  await ready(page, context, harness);
  await fillCreate(page, { dest: "LON", month: "2027-03", target: "12000" });
  await createForm(page).getByRole("button", { name: "開始追蹤" }).dblclick();
  await expect(page.getByText("我的航線訂閱：2 / 10")).toBeVisible();
  await card(page, OSA).getByRole("button", { name: "暫停追蹤" }).dblclick();
  await expect(card(page, OSA).getByRole("button", { name: "恢復追蹤" })).toBeVisible();
  expect(calls(net, page, "POST")).toHaveLength(1);
  expect(calls(net, page, "PATCH")).toHaveLength(1);
  expect(await harness.count("POST /subscriptions")).toBe(1);
  expect(await harness.count("PATCH /subscriptions/{id}")).toBe(1);
});
