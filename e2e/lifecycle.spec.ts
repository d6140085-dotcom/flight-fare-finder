// The full subscription lifecycle in Chromium, checked against both the rendered page and the
// stored records (DynamoDB Local in CI). Every returned version must equal the stored version.
import type { Page, Response } from "@playwright/test";
import {
  card,
  createForm,
  expect,
  fillCreate,
  monthLabel,
  openApp,
  signIn,
  test,
  type Harness,
} from "./fixtures";

const M14_STAMP = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/;

async function write(page: Page, harness: Harness, method: string, act: () => Promise<void>) {
  const [res] = await Promise.all([
    page.waitForResponse(
      (r: Response) => r.url().startsWith(harness.apiUrl) && r.request().method() === method,
    ),
    act(),
  ]);
  return res;
}

async function expectStored(harness: Harness, body: { id: string; version: string }) {
  const rows = await harness.rows();
  const row = rows.find((r) => r.route.replace("#", "_") === body.id);
  expect(row, `stored row for ${body.id}`).toBeDefined();
  expect(row!.version_token).toBe(body.version); // returned version == stored version
  expect(row!.updated_at).toMatch(M14_STAMP);
  return row!;
}

test("list → create → edit price → pause → resume → month move → route move → delete", async ({
  page,
  context,
  harness,
}) => {
  await harness.reset(["legacy_rolling_tokyo"]);
  await signIn(context);
  await openApp(page, 1);
  await expect(card(page, "台北 → 東京・每月滾動（2026 年 12 月）")).toBeVisible();

  // Create a fixed-month subscription.
  await fillCreate(page, { dest: "LON", month: "2027-03", target: "12000" });
  let res = await write(page, harness, "POST", () =>
    createForm(page).getByRole("button", { name: "開始追蹤" }).click(),
  );
  expect(res.status()).toBe(201);
  let body = await res.json();
  expect(body.id).toBe("TPE-LON_2027-03");
  const lon = `台北 → 倫敦・${monthLabel("2027-03")}`;
  await expect(card(page, lon)).toBeVisible();
  await expect(page.getByText("我的航線訂閱：2 / 10")).toBeVisible();
  await expectStored(harness, body);
  expect((await harness.state()).quota["user@example.com"]?.count).toBe(2);

  // Edit the target price.
  await card(page, lon).getByRole("button", { name: "編輯" }).click();
  const edit = card(page, lon).getByRole("form", { name: "編輯訂閱" });
  await edit.getByLabel("目標價（新台幣）").fill("11000");
  res = await write(page, harness, "PATCH", () =>
    edit.getByRole("button", { name: "儲存變更" }).click(),
  );
  expect(res.status()).toBe(200);
  body = await res.json();
  expect(body.target_price).toBe(11000);
  expect((await expectStored(harness, body)).target_price).toBe(11000);
  await expect(card(page, lon).getByRole("form", { name: "編輯訂閱" })).toHaveCount(0);

  // Pause, then resume.
  res = await write(page, harness, "PATCH", () =>
    card(page, lon).getByRole("button", { name: "暫停追蹤" }).click(),
  );
  body = await res.json();
  expect(body.status).toBe("paused");
  expect((await expectStored(harness, body)).alerts_paused).toBe(true);
  await expect(card(page, lon).getByRole("button", { name: "恢復追蹤" })).toBeVisible();
  res = await write(page, harness, "PATCH", () =>
    card(page, lon).getByRole("button", { name: "恢復追蹤" }).click(),
  );
  body = await res.json();
  expect(body.status).toBe("active");
  // Contract §3.1: alerts_paused is stored only when true, so resuming removes it.
  expect(await expectStored(harness, body)).not.toHaveProperty("alerts_paused");

  // Change the departure month: a new subscription ID.
  await card(page, lon).getByRole("button", { name: "編輯" }).click();
  await card(page, lon)
    .getByRole("form", { name: "編輯訂閱" })
    .getByLabel("出發月份")
    .selectOption("2027-05");
  res = await write(page, harness, "PATCH", () =>
    card(page, lon).getByRole("button", { name: "儲存變更" }).click(),
  );
  body = await res.json();
  expect(body.id).toBe("TPE-LON_2027-05");
  const may = `台北 → 倫敦・${monthLabel("2027-05")}`;
  await expect(card(page, may)).toBeVisible();
  await expect(card(page, lon)).toHaveCount(0);
  await expectStored(harness, body);
  expect((await harness.rows()).map((r) => r.route).sort()).toEqual(["TPE-LON#2027-05", "TPE-TYO"]);

  // Change the route (destination): another new ID.
  await card(page, may).getByRole("button", { name: "編輯" }).click();
  const box = card(page, may).getByRole("form", { name: "編輯訂閱" }).getByRole("combobox", {
    name: "目的地",
  });
  await box.fill("PAR");
  await box.press("Enter");
  res = await write(page, harness, "PATCH", () =>
    card(page, may).getByRole("button", { name: "儲存變更" }).click(),
  );
  body = await res.json();
  expect(body.id).toBe("TPE-PAR_2027-05");
  const par = `台北 → 巴黎・${monthLabel("2027-05")}`;
  await expect(card(page, par)).toBeVisible();
  await expect(card(page, may)).toHaveCount(0);
  await expectStored(harness, body);
  expect((await harness.rows()).map((r) => r.route).sort()).toEqual(["TPE-PAR#2027-05", "TPE-TYO"]);

  // Delete it (the request goes to the newest ID with its newest version).
  await card(page, par).getByRole("button", { name: "刪除" }).click();
  res = await write(page, harness, "DELETE", () =>
    page.getByRole("button", { name: "確認刪除" }).click(),
  );
  expect(res.status()).toBe(204);
  expect(new URL(res.url()).pathname).toBe("/subscriptions/TPE-PAR_2027-05");
  expect(new URL(res.url()).searchParams.get("if_version")).toBe(body.version);
  await expect(card(page, par)).toHaveCount(0);
  await expect(page.getByText("我的航線訂閱：1 / 10")).toBeVisible();
  expect((await harness.rows()).map((r) => r.route)).toEqual(["TPE-TYO"]);
  expect((await harness.state()).quota["user@example.com"]?.count).toBe(1);
});
