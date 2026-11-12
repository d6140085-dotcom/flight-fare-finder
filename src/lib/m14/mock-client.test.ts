import { describe, expect, it } from "vitest";

import { ApiError, apiErrorMessage, parseFareCheck } from "./api";
import { MockSubscriptionsClient, seedRows } from "./mock-client";
import { ORIGINS, DESTINATIONS } from "./catalog";

// 2026-11-11 10:00 Taipei: selectable months 2026-12 … 2027-11; rolling rows check 2026-12.
const NOW = new Date("2026-11-11T02:00:00Z");
const mock = (scenario: "demo" | "empty" | "full" | "multi_month" = "demo") =>
  new MockSubscriptionsClient({ now: () => NOW, scenario });

async function code(p: Promise<unknown>) {
  try {
    await p;
  } catch (e) {
    return e instanceof ApiError ? e.code : "not-an-api-error";
  }
  return "no-error";
}

describe("mock subscriptions API (contract docs/api/m1.4-subscriptions-api.md)", () => {
  it("lists derived fields, limits and selectable months (§4.1)", async () => {
    const res = await mock().list();
    expect(res.limits).toEqual({ max: 10, used: 6 });
    expect(res.months.current).toBe("2026-11");
    expect(res.months.selectable[0]).toBe("2026-12");
    expect(res.months.selectable).toHaveLength(12);
    const tyo = res.subscriptions.find((s) => s.route === "TPE-TYO");
    expect(tyo).toMatchObject({
      month_mode: "rolling",
      departure_month: null,
      effective_month: "2026-12",
      status: "active",
      plan_name: "tokyo",
      origin_name: { zh: "台北", en: "Taipei" },
    });
    expect(tyo?.fare_check).toMatchObject({ kind: "ok", cheapest_twd: 8062 });
    const statuses = Object.fromEntries(res.subscriptions.map((s) => [s.route, s.status]));
    expect(statuses).toMatchObject({ "KHH-OKA": "paused", "RMQ-SEL": "expired" });
  });

  it("uses only catalog codes and fake data in every scenario", () => {
    for (const s of ["demo", "empty", "full", "list_error", "unauthorized"] as const) {
      for (const r of seedRows(s, NOW)) {
        expect(ORIGINS.some((o) => o.code === r.origin)).toBe(true);
        expect(DESTINATIONS.some((d) => d.code === r.destination)).toBe(true);
        expect(JSON.stringify(r)).not.toMatch(/@/);
      }
    }
  });

  it("validates creates in the documented order and requires a month (§4.2)", async () => {
    const c = mock("empty");
    const base = {
      origin: "KHH",
      destination: "OKA",
      departure_month: "2027-01",
      target_price: 4500,
    };
    expect(await code(c.create({ ...base, origin: "TSA" }))).toBe("unsupported_origin");
    expect(await code(c.create({ ...base, destination: "HND" }))).toBe("unsupported_destination");
    expect(await code(c.create({ ...base, departure_month: "2026-11" }))).toBe(
      "invalid_departure_month",
    );
    expect(await code(c.create({ ...base, departure_month: "" }))).toBe("invalid_departure_month");
    expect(await code(c.create({ ...base, target_price: 0 }))).toBe("invalid_target_price");
    expect(await code(c.create({ ...base, target_price: 12.5 }))).toBe("invalid_target_price");
    const created = await c.create(base);
    expect(created).toMatchObject({
      id: "KHH-OKA_2027-01",
      route: "KHH-OKA",
      month_mode: "fixed",
      alerts_paused: false,
      fare_check: null,
    });
    expect(await code(c.create(base))).toBe("duplicate_subscription");
    try {
      await c.create(base);
    } catch (e) {
      expect((e as ApiError).details).toEqual({ route: "KHH-OKA", departure_month: "2027-01" });
    }
  });

  it("holds several months of one route as independent subscriptions (R-MM-1)", async () => {
    const c = mock("empty");
    const ids = [];
    for (const m of ["2027-01", "2027-02", "2027-03"]) {
      ids.push(
        (
          await c.create({
            origin: "KHH",
            destination: "TYO",
            departure_month: m,
            target_price: 9000,
          })
        ).id,
      );
    }
    expect(ids).toEqual(["KHH-TYO_2027-01", "KHH-TYO_2027-02", "KHH-TYO_2027-03"]);
    expect((await c.list()).limits.used).toBe(3);
    await c.update("KHH-TYO_2027-02", { alerts_paused: true });
    await c.remove("KHH-TYO_2027-01");
    const left = (await c.list()).subscriptions.map((s) => [s.id, s.status]);
    expect(left).toEqual([
      ["KHH-TYO_2027-02", "paused"],
      ["KHH-TYO_2027-03", "active"],
    ]);
    // Moving a month onto a sibling month is a duplicate; onto a free month it moves the ID.
    expect(await code(c.update("KHH-TYO_2027-03", { departure_month: "2027-02" }))).toBe(
      "duplicate_subscription",
    );
    const moved = await c.update("KHH-TYO_2027-03", { departure_month: "2027-05" });
    expect(moved.id).toBe("KHH-TYO_2027-05");
    expect(await code(c.update("KHH-TYO_2027-03", { target_price: 1 }))).toBe("not_found");
  });

  it("keeps the rolling row beside a fixed row of the same month", async () => {
    const c = mock("multi_month");
    const subs = (await c.list()).subscriptions;
    expect(subs.map((s) => [s.id, s.effective_month, s.status])).toEqual([
      ["KHH-TYO_2027-01", "2027-01", "active"],
      ["KHH-TYO_2027-02", "2027-02", "active"],
      ["KHH-TYO_2027-03", "2027-03", "paused"],
      ["TPE-TYO", "2026-12", "active"],
      ["TPE-TYO_2026-12", "2026-12", "active"],
    ]);
    // Both resolve to the same route/month fare check.
    const tyo = subs.filter((s) => s.route === "TPE-TYO").map((s) => s.fare_check?.kind);
    expect(tyo).toEqual(["ok", "ok"]);
    expect(await code(c.update("TPE-TYO", { departure_month: "2026-12" }))).toBe(
      "duplicate_subscription",
    );
  });

  it("enforces 10 total records; pausing frees nothing, deleting frees one (§5)", async () => {
    const c = mock("full");
    expect((await c.list()).limits).toEqual({ max: 10, used: 10 });
    const req = {
      origin: "RMQ",
      destination: "OKA",
      departure_month: "2027-01",
      target_price: 3000,
    };
    expect(await code(c.create(req))).toBe("limit_reached");
    await c.update("TPE-TYO_2026-12", { alerts_paused: true });
    expect(await code(c.create(req))).toBe("limit_reached");
    expect(await code(c.create({ ...req, origin: "TPE", destination: "TYO" }))).toBe(
      "limit_reached", // another month of an existing route counts as a new record
    );
    await c.remove("TPE-TYO_2026-12");
    await c.create(req);
    expect((await c.list()).limits.used).toBe(10);
  });

  it("keeps a legacy rolling row rolling unless a month is chosen, then never back (§3.2)", async () => {
    const c = mock();
    const kept = await c.update("TPE-TYO", { target_price: 8500 });
    expect(kept).toMatchObject({ month_mode: "rolling", target_price: 8500, plan_name: "tokyo" });
    const fixed = await c.update("TPE-TYO", { departure_month: "2027-03" });
    expect(fixed).toMatchObject({
      id: "TPE-TYO_2027-03",
      month_mode: "fixed",
      departure_month: "2027-03",
      plan_name: null,
    });
    expect(fixed.fare_check).toBeNull(); // a new month starts unchecked
    expect(await code(c.update("TPE-TYO_2027-03", { departure_month: null }))).toBe(
      "invalid_departure_month",
    );
    expect(await code(c.update("TPE-TYO", { target_price: 1 }))).toBe("not_found");
  });

  it("reactivates an expired row by choosing a month in the window (§3.3)", async () => {
    const c = mock();
    expect(await code(c.update("RMQ-SEL_2026-10", { departure_month: "2026-10" }))).toBe(
      "no-error",
    );
    const row = await c.update("RMQ-SEL_2026-10", { departure_month: "2027-02" });
    expect([row.id, row.status]).toEqual(["RMQ-SEL_2027-02", "active"]);
  });

  it("changes a route atomically and rejects a change onto an existing route (§6)", async () => {
    const c = mock();
    const lon = "TPE-LON_2027-02";
    expect(await code(c.update(lon, { origin: "TPE" }))).toBe("route_change_incomplete");
    expect(
      await code(c.update(lon, { origin: "TPE", destination: "SYD", departure_month: "2026-12" })),
    ).toBe("duplicate_subscription");
    const moved = await c.update(lon, { origin: "TPE", destination: "PAR" });
    expect([moved.id, moved.route]).toEqual(["TPE-PAR_2027-02", "TPE-PAR"]);
    const routes = (await c.list()).subscriptions.map((s) => s.route);
    expect(routes).toContain("TPE-PAR");
    expect(routes).not.toContain("TPE-LON");
    expect((await c.list()).limits.used).toBe(6);
  });

  it("detects concurrent changes with if_updated_at and reports missing rows (§4.3, §4.4)", async () => {
    const c = mock();
    const before = (await c.list()).subscriptions.find((s) => s.route === "TPE-SYD");
    const syd = "TPE-SYD_2026-12";
    c.touch(syd);
    expect(
      await code(c.update(syd, { target_price: 1, if_updated_at: before?.updated_at ?? "" })),
    ).toBe("conflict");
    expect(await code(c.remove(syd, before?.updated_at))).toBe("conflict");
    expect(await code(c.remove("TPE-ZZZ"))).toBe("not_found");
    for (const bad of ["bad", "TPE-SYD#2026-12", "TPE-SYD%232026-12", "TPE-SYD_2026-1"]) {
      expect(await code(c.remove(bad))).toBe("invalid_route");
    }
    expect(await code(c.update(syd, {}))).toBe("empty_patch");
  });

  it("simulates list failures without touching the network", async () => {
    const c = mock();
    c.setScenario("list_error");
    expect(await code(c.list())).toBe("internal_error");
    c.setScenario("unauthorized");
    expect(await code(c.list())).toBe("unauthorized");
  });
});

describe("fare_check parsing never turns missing data into NT$0", () => {
  const at = "2026-11-11T02:30:00Z";
  it("accepts the three API kinds", () => {
    expect(parseFareCheck({ kind: "no_data", checked_at: at })).toEqual({
      kind: "no_data",
      checked_at: at,
    });
    expect(parseFareCheck({ kind: "error", checked_at: at })?.kind).toBe("error");
    expect(
      parseFareCheck({
        kind: "ok",
        checked_at: at,
        cheapest_twd: 8062,
        depart_date: "2026-12-11",
        return_date: "2026-12-18",
      }),
    ).toMatchObject({ kind: "ok", cheapest_twd: 8062, depart_date: "2026-12-11" });
  });

  it("downgrades a zero, negative, fractional, missing or non-TWD price to an error", () => {
    for (const bad of [0, -1, 12.5, undefined, null, "8062", Number.NaN]) {
      expect(parseFareCheck({ kind: "ok", checked_at: at, cheapest_twd: bad })?.kind).toBe("error");
    }
    expect(
      parseFareCheck({ kind: "ok", checked_at: at, cheapest_twd: 100, currency: "USD" })?.kind,
    ).toBe("error");
  });

  it("ignores unknown kinds and missing timestamps", () => {
    expect(parseFareCheck(null)).toBeNull();
    expect(parseFareCheck({ kind: "maybe", checked_at: at })).toBeNull();
    expect(parseFareCheck({ kind: "no_data" })).toBeNull();
  });

  it("maps API errors to Traditional Chinese without echoing server text", () => {
    expect(apiErrorMessage(new ApiError("limit_reached", 409))).toContain("最多 10 筆");
    expect(apiErrorMessage(new ApiError("conflict", 409))).toContain("重新載入");
    expect(apiErrorMessage(new Error("secret token eyJabc"))).not.toContain("eyJ");
  });
});
