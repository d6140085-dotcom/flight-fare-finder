// Phase 3B-E: the backend's session check (Supabase /auth/v1/user) fails while the browser's own
// session is fine. The API answers 503 auth_unavailable, so the page must show "service
// unavailable" — not "sign in again" — and no write may happen. A session Supabase really
// rejects is still a sign-in problem.
//
// Only the harness's fake Supabase (the backend's) fails here; the browser's Supabase (fake,
// fixtures.ts) keeps answering, so the app's route guard lets the user in as before.
import type { Page } from "@playwright/test";
import {
  APP,
  FAKE_TOKENS,
  STORAGE_KEY,
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
  type SupabaseFault,
} from "./fixtures";

const OSA = `台北 → 大阪・${monthLabel("2027-01")}`;
const UNAVAILABLE = "訂閱服務暫時無法使用";
const UNCERTAIN = "可能已完成，也可能沒有";
const SIGN_IN_AGAIN = "登入已過期";

async function expectNoSignInPrompt(page: Page) {
  await expect(page).toHaveURL(new RegExp(`${APP}$`));
  await expect(page.getByText(SIGN_IN_AGAIN)).toHaveCount(0);
  await expect(page.getByRole("link", { name: "重新登入" })).toHaveCount(0);
  // The browser session is untouched (nothing signed the user out).
  expect(await page.evaluate((k) => window.localStorage.getItem(k), STORAGE_KEY)).not.toBeNull();
}

async function expectNoDynamoDb(harness: Harness, route: string, n: number) {
  const mine = (await harness.state()).handler_calls.filter((c) => c.route === route);
  expect(mine).toHaveLength(n);
  for (const c of mine) expect(c).toEqual({ route, status: 503, ddb: [] });
}

test("a Supabase outage on load shows 'service unavailable', not 'sign in again'", async ({
  page,
  context,
  harness,
  net,
}) => {
  await harness.reset(["fixed_jan"]);
  const before = await harness.state();
  await signIn(context);
  await harness.supabaseFault(503, 2); // the first load and the list's one automatic retry
  await page.goto(APP);

  const alert = page.getByRole("alert").filter({ hasText: UNAVAILABLE });
  await expect(alert).toBeVisible();
  await expect(alert.getByRole("button", { name: "重試" })).toBeVisible();
  await expectNoSignInPrompt(page);
  expect(net.supabaseUserCalls).toContain(FAKE_TOKENS.valid); // the route guard still checked the session
  expect(calls(net, page, "GET")).toHaveLength(2);
  await expectNoDynamoDb(harness, "GET /subscriptions", 2);

  // Supabase is back: a manual retry loads the list with the same session.
  await alert.getByRole("button", { name: "重試" }).click();
  await expect(card(page, OSA)).toBeVisible();
  await expect(page.getByText("我的航線訂閱：1 / 10")).toBeVisible();
  const after = await harness.state();
  expect(after.rows).toEqual(before.rows);
  expect(after.quota).toEqual(before.quota);
});

/** Opens the page normally, then makes the backend's next session check fail with `fault`. */
async function readyThenOutage(
  page: Page,
  context: Parameters<typeof signIn>[0],
  harness: Harness,
  fault: SupabaseFault,
) {
  await harness.reset(["fixed_jan"]);
  await signIn(context);
  await openApp(page, 1);
  const before = await harness.state();
  await harness.supabaseFault(fault, 1);
  return before;
}

/** The write's 503 is uncertain to the page: it reloads the list once and resends nothing. */
async function expectOneReloadNoWrite(
  page: Page,
  net: Net,
  harness: Harness,
  before: Awaited<ReturnType<Harness["state"]>>,
  act: () => Promise<unknown>,
) {
  const reload = page.waitForResponse((r) => r.request().method() === "GET");
  await act();
  await reload;
  await expect(page.getByText(UNCERTAIN)).toBeVisible();
  expect(calls(net, page, "GET")).toHaveLength(2); // initial load + exactly one reload
  await expectNoSignInPrompt(page);
  const after = await harness.state();
  expect(after.rows).toEqual(before.rows); // nothing written
  expect(after.quota).toEqual(before.quota);
}

test("Supabase 503 during an edit: no write, no replay, no sign-out, input kept", async ({
  page,
  context,
  harness,
  net,
}) => {
  const before = await readyThenOutage(page, context, harness, 503);
  await card(page, OSA).getByRole("button", { name: "編輯" }).click();
  const edit = card(page, OSA).getByRole("form", { name: "編輯訂閱" });
  await edit.getByLabel("目標價（新台幣）").fill("7777");
  await expectOneReloadNoWrite(page, net, harness, before, () =>
    edit.getByRole("button", { name: "儲存變更" }).click(),
  );
  expect(calls(net, page, "PATCH")).toHaveLength(1);
  await expectNoDynamoDb(harness, "PATCH /subscriptions/{id}", 1);
  await expect(edit.getByLabel("目標價（新台幣）")).toHaveValue("7777");
});

test("Supabase connection reset during a delete: no write, no replay, row intact", async ({
  page,
  context,
  harness,
  net,
}) => {
  const before = await readyThenOutage(page, context, harness, "reset");
  await card(page, OSA).getByRole("button", { name: "刪除" }).click();
  await expectOneReloadNoWrite(page, net, harness, before, () =>
    page.getByRole("button", { name: "確認刪除" }).click(),
  );
  expect(calls(net, page, "DELETE")).toHaveLength(1);
  await expectNoDynamoDb(harness, "DELETE /subscriptions/{id}", 1);
  await expect(card(page, OSA)).toBeVisible();
});

test("Supabase timeout during a create: no write, no quota change, input kept", async ({
  page,
  context,
  harness,
  net,
}) => {
  const before = await readyThenOutage(page, context, harness, "timeout");
  await fillCreate(page, { dest: "LON", month: "2027-03", target: "12000" });
  await expectOneReloadNoWrite(page, net, harness, before, () =>
    createForm(page).getByRole("button", { name: "開始追蹤" }).click(),
  );
  expect(calls(net, page, "POST")).toHaveLength(1);
  await expectNoDynamoDb(harness, "POST /subscriptions", 1);
  await expect(createForm(page).getByLabel("目標價（新台幣）")).toHaveValue("12000");
  await expect(createForm(page).getByLabel("出發月份")).toHaveValue("2027-03");
});

test("a session Supabase rejects is still a sign-in problem (401, no retry, no data access)", async ({
  page,
  context,
  harness,
  net,
}) => {
  await harness.reset(["fixed_jan"]);
  await signIn(context);
  await harness.supabaseFault(401, 1);
  await page.goto(APP);
  await expect(page.getByRole("alert").filter({ hasText: SIGN_IN_AGAIN })).toBeVisible();
  await expect(page.getByRole("link", { name: "重新登入" })).toBeVisible();
  expect(calls(net, page, "GET")).toHaveLength(1); // a 401 is final: no automatic retry
  const mine = (await harness.state()).handler_calls;
  expect(mine).toEqual([{ route: "GET /subscriptions", status: 401, ddb: [] }]);
});
