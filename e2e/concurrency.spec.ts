// Two tabs of the same user read the same version; tab A writes; tab B's stale write gets 409,
// tab B reloads exactly once and never resends. Synchronisation is explicit: every step waits
// for the specific response or request count it depends on.
import type { Page } from "@playwright/test";
import {
  NOW,
  ROWS,
  calls,
  card,
  expect,
  monthLabel,
  openApp,
  signIn,
  test,
  type Net,
} from "./fixtures";

const OSA = `台北 → 大阪・${monthLabel("2027-01")}`;
const OKA = `高雄 → 沖繩・${monthLabel("2027-02")}`;
const V1 = ROWS["fixed_jan"]!["version_token"] as string;

async function twoTabs(page: Page, context: Parameters<typeof signIn>[0], net: Net) {
  await signIn(context);
  const b = await context.newPage();
  await b.clock.setFixedTime(NOW);
  await openApp(page, 2);
  await openApp(b, 2);
  // Both tabs read the list exactly once, so both hold version V1.
  expect(calls(net, page, "GET")).toHaveLength(1);
  expect(calls(net, b, "GET")).toHaveLength(1);
  return b;
}

test("stale PATCH: tab B gets 409, reloads once, does not resend; tab A's change stays", async ({
  page,
  context,
  harness,
  net,
}) => {
  await harness.reset(["fixed_jan", "paused_feb"]);
  const b = await twoTabs(page, context, net);

  // Tab A changes the price (V1 -> V2).
  await card(page, OSA).getByRole("button", { name: "編輯" }).click();
  await card(page, OSA)
    .getByRole("form", { name: "編輯訂閱" })
    .getByLabel("目標價（新台幣）")
    .fill("7000");
  const [a] = await Promise.all([
    page.waitForResponse((r) => r.request().method() === "PATCH"),
    card(page, OSA).getByRole("button", { name: "儲存變更" }).click(),
  ]);
  expect(a.status()).toBe(200);
  const v2 = (await a.json()).version as string;
  expect(v2).not.toBe(V1);

  // Tab B still shows V1 and tries its own change.
  await card(b, OSA).getByRole("button", { name: "編輯" }).click();
  const editB = card(b, OSA).getByRole("form", { name: "編輯訂閱" });
  await editB.getByLabel("目標價（新台幣）").fill("6500");
  const reload = b.waitForResponse((r) => r.request().method() === "GET");
  const [stale] = await Promise.all([
    b.waitForResponse((r) => r.request().method() === "PATCH"),
    editB.getByRole("button", { name: "儲存變更" }).click(),
  ]);
  expect(stale.status()).toBe(409);
  expect(JSON.parse(stale.request().postData() ?? "{}").if_version).toBe(V1);
  await reload; // the one reload
  await expect(b.getByText("這筆訂閱剛被其他裝置或分頁修改")).toBeVisible();

  expect(calls(net, b, "PATCH")).toHaveLength(1); // never resent
  expect(calls(net, b, "GET")).toHaveLength(2); // initial + exactly one reload
  expect(await harness.count("PATCH /subscriptions/{id}")).toBe(2); // A's and B's, nothing else
  const stored = (await harness.rows()).find((r) => r.route === "TPE-OSA#2027-01")!;
  expect(stored.target_price).toBe(7000);
  expect(stored.version_token).toBe(v2);
  // Tab B kept what was typed, so the user can decide whether to try again.
  await expect(editB.getByLabel("目標價（新台幣）")).toHaveValue("6500");
});

test("stale DELETE: tab B gets 409, reloads once, nothing is deleted", async ({
  page,
  context,
  harness,
  net,
}) => {
  await harness.reset(["fixed_jan", "paused_feb"]);
  const before = await harness.rows();
  const b = await twoTabs(page, context, net);

  // Tab A pauses the Osaka row (V1 -> V2).
  const [a] = await Promise.all([
    page.waitForResponse((r) => r.request().method() === "PATCH"),
    card(page, OSA).getByRole("button", { name: "暫停追蹤" }).click(),
  ]);
  expect(a.status()).toBe(200);
  const v2 = (await a.json()).version as string;

  // Tab B deletes it with V1.
  await card(b, OSA).getByRole("button", { name: "刪除" }).click();
  const reload = b.waitForResponse((r) => r.request().method() === "GET");
  const [stale] = await Promise.all([
    b.waitForResponse((r) => r.request().method() === "DELETE"),
    b.getByRole("button", { name: "確認刪除" }).click(),
  ]);
  expect(stale.status()).toBe(409);
  expect(new URL(stale.url()).searchParams.get("if_version")).toBe(V1);
  await reload;
  await expect(b.getByText("這筆訂閱剛被其他裝置或分頁修改")).toBeVisible();
  await expect(card(b, OSA).getByText("已暫停").first()).toBeVisible(); // B now shows A's change

  expect(calls(net, b, "DELETE")).toHaveLength(1);
  expect(calls(net, b, "GET")).toHaveLength(2);
  expect(await harness.count("DELETE /subscriptions/{id}")).toBe(1);
  const after = await harness.rows();
  expect(after.map((r) => r.route).sort()).toEqual(["KHH-OKA#2027-02", "TPE-OSA#2027-01"]);
  expect(after.find((r) => r.route === "TPE-OSA#2027-01")!.version_token).toBe(v2);
  // The unrelated row is untouched.
  expect(after.find((r) => r.route === "KHH-OKA#2027-02")).toEqual(
    before.find((r) => r.route === "KHH-OKA#2027-02"),
  );
  await expect(card(b, OKA)).toBeVisible();
});
