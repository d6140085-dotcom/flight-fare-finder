// Subscription rules through the browser: months, the 10-row limit (paused and expired count),
// legacy rolling rows, server months, status labels, quota records and duplicates.
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
} from "./fixtures";

test("several departure months for one route; duplicates are refused by the server", async ({
  page,
  context,
  harness,
  net,
}) => {
  await signIn(context);
  await openApp(page, 0);
  for (const [i, m] of ["2027-01", "2027-02", "2027-03"].entries()) {
    await fillCreate(page, { dest: "LON", month: m, target: "12000" });
    await createForm(page).getByRole("button", { name: "開始追蹤" }).click();
    await expect(page.getByText(`我的航線訂閱：${i + 1} / 10`)).toBeVisible();
  }
  for (const m of ["2027-01", "2027-02", "2027-03"]) {
    await expect(card(page, `台北 → 倫敦・${monthLabel(m)}`)).toBeVisible();
  }
  expect((await harness.rows()).map((r) => r.route).sort()).toEqual([
    "TPE-LON#2027-01",
    "TPE-LON#2027-02",
    "TPE-LON#2027-03",
  ]);
  expect((await harness.state()).quota["user@example.com"]?.count).toBe(3);

  // The page's own check refuses a duplicate it can see, without a request.
  await fillCreate(page, { dest: "LON", month: "2027-02", target: "9000" });
  await createForm(page).getByRole("button", { name: "開始追蹤" }).click();
  await expect(page.getByText(/同一航線同一月份只能訂閱一次/)).toBeVisible();
  expect(calls(net, page, "POST")).toHaveLength(3);

  // A duplicate the page could not see (created in another tab) is refused by the server.
  const other = await context.newPage();
  await other.clock.setFixedTime(new Date("2026-11-05T03:00:00Z"));
  await openApp(other, 3);
  await fillCreate(page, { dest: "OSA", month: "2027-04", target: "8000" });
  await fillCreate(other, { dest: "OSA", month: "2027-04", target: "8000" });
  await createForm(other).getByRole("button", { name: "開始追蹤" }).click();
  await expect(other.getByText("我的航線訂閱：4 / 10")).toBeVisible();
  const getsBefore = calls(net, page, "GET").length;
  const [dup] = await Promise.all([
    page.waitForResponse((r) => r.request().method() === "POST"),
    createForm(page).getByRole("button", { name: "開始追蹤" }).click(),
  ]);
  expect(dup.status()).toBe(409);
  expect((await dup.json()).error).toBe("duplicate_subscription");
  await expect(
    page.getByRole("alert").filter({ hasText: "同一航線同一月份只能訂閱一次" }),
  ).toBeVisible();
  expect(calls(net, page, "GET")).toHaveLength(getsBefore); // no reload for a rejection
  const routes = (await harness.rows()).map((r) => r.route);
  expect(routes.filter((r) => r === "TPE-OSA#2027-04")).toHaveLength(1);
  expect((await harness.state()).quota["user@example.com"]?.count).toBe(4);
});

test("ten rows at most; paused, expired and legacy rolling rows count and show their status", async ({
  page,
  context,
  harness,
}) => {
  await harness.reset([
    "legacy_rolling_tokyo",
    "expired_oct",
    "paused_feb",
    "limit_01",
    "limit_02",
    "limit_03",
    "limit_04",
    "limit_05",
    "limit_06",
  ]);
  await signIn(context);
  await openApp(page, 9);
  await expect(
    card(page, "台北 → 東京・每月滾動（2026 年 12 月）").getByText("追蹤中").first(),
  ).toBeVisible();
  await expect(
    card(page, `高雄 → 沖繩・${monthLabel("2027-02")}`)
      .getByText("已暫停")
      .first(),
  ).toBeVisible();
  await expect(
    card(page, `台中 → 首爾・${monthLabel("2026-10")}`)
      .getByText("已過期")
      .first(),
  ).toBeVisible();
  // limit_01 and limit_04 are seeded paused.
  await expect(
    card(page, `台北 → 曼谷・${monthLabel("2026-12")}`)
      .getByText("已暫停")
      .first(),
  ).toBeVisible();

  await fillCreate(page, { dest: "LON", month: "2027-06", target: "12000" });
  await createForm(page).getByRole("button", { name: "開始追蹤" }).click();
  await expect(page.getByText("我的航線訂閱：10 / 10")).toBeVisible();
  await expect(page.getByTestId("limit-reached")).toContainText("已達 10");
  await expect(createForm(page).getByRole("button", { name: "開始追蹤" })).toBeDisabled();
  const st = await harness.state();
  expect(st.rows["user@example.com"]).toHaveLength(10);
  expect(st.quota["user@example.com"]?.count).toBe(10);
});

test("the create form offers exactly the server's selectable months", async ({
  page,
  context,
  harness,
}) => {
  await signIn(context);
  const [res] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().startsWith(harness.apiUrl) && r.request().method() === "GET",
    ),
    page.goto("/app/subscriptions"),
  ]);
  const body = await res.json();
  expect(body.months.current).toBe("2026-11");
  await expect(page.getByText("我的航線訂閱：0 / 10")).toBeVisible();
  const options = await createForm(page)
    .getByLabel("出發月份")
    .locator("option")
    .evaluateAll((els) => els.map((e) => (e as HTMLOptionElement).value));
  expect(options).toEqual(body.months.selectable);
  expect(options[0]).toBe("2026-12");
  expect(options).toHaveLength(12);
});
