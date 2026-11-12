import { describe, expect, it } from "vitest";

import { MAX_TARGET_TWD } from "@/lib/flight-api";
import { DESTINATIONS, ORIGINS, POLICY, routeLabel } from "./catalog";
import {
  ROLLING,
  demoSubscriptions,
  errorMessage,
  reducer,
  statusOf,
  validate,
  type RouteSubscription,
  type State,
} from "./subscriptions";
import { subscriptionId } from "./subscription-id";

// 2026-11-05 11:00 in Taipei: current month 2026-11, selectable 2026-12 … 2027-11.
const NOW = new Date("2026-11-05T03:00:00Z");
const STAMP = NOW.toISOString();
const sub = (route: string, extra: Partial<RouteSubscription> = {}): RouteSubscription => {
  const [origin = "", destination = ""] = route.split("-");
  const row: Omit<RouteSubscription, "id"> = {
    route,
    origin,
    destination,
    departure_month: "2026-12",
    target_price: 9000,
    currency: "TWD",
    alerts_paused: false,
    plan_name: null,
    created_at: STAMP,
    updated_at: STAMP,
    ...extra,
  };
  return { id: subscriptionId(origin, destination, row.departure_month), ...row };
};
const state = (subs: RouteSubscription[]): State => ({ subs, lastError: null });
const ten = (extra: Partial<RouteSubscription> = {}) =>
  DESTINATIONS.slice(0, 10).map((d) => sub(`TPE-${d.code}`, extra));

describe("catalog and policy", () => {
  it("offers the three origins and the worldwide destination catalog from airports.json", () => {
    expect(ORIGINS.map((a) => a.code)).toEqual(["TPE", "KHH", "RMQ"]);
    expect(DESTINATIONS.length).toBeGreaterThanOrEqual(80);
    expect(DESTINATIONS.length).toBeLessThanOrEqual(100);
    expect(routeLabel("TPE", "TYO")).toBe("台北 → 東京");
  });

  it("uses a 10-record total limit, the M1 price range and a 12-month window", () => {
    expect(POLICY).toEqual({
      maxSubscriptions: 10,
      minTarget: 1,
      maxTarget: 1_000_000,
      firstMonthOffset: 1,
      monthWindow: 12,
      timeZone: "Asia/Taipei",
    });
    expect(POLICY.maxTarget).toBe(MAX_TARGET_TWD);
  });
});

describe("status", () => {
  it("is expired once the fixed month has passed, whatever the pause state", () => {
    expect(statusOf(sub("TPE-TYO"), NOW)).toBe("active");
    expect(statusOf(sub("TPE-TYO", { alerts_paused: true }), NOW)).toBe("paused");
    expect(statusOf(sub("TPE-TYO", { departure_month: "2026-11" }), NOW)).toBe("active"); // this month
    expect(statusOf(sub("TPE-TYO", { departure_month: "2026-10" }), NOW)).toBe("expired");
    expect(statusOf(sub("TPE-TYO", { departure_month: "2026-10", alerts_paused: true }), NOW)).toBe(
      "expired",
    );
  });

  it("never expires a legacy rolling (no month) subscription", () => {
    expect(
      statusOf(sub("TPE-TYO", { departure_month: null }), new Date("2030-01-01T00:00:00Z")),
    ).toBe("active");
  });
});

describe("validate", () => {
  const ok = { origin: "TPE", destination: "TYO", month: "2026-12", targetText: "9,000" };

  it("accepts a valid new route with a month and parses the target like M1 does", () => {
    expect(validate([], ok, { now: NOW })).toEqual({
      ok: true,
      value: { origin: "TPE", destination: "TYO", departure_month: "2026-12", target: 9000 },
    });
  });

  it.each(["LON", "PAR", "NYC", "LAX", "YVR", "SAO", "SYD", "MEL", "AKL", "ZQN", "DXB", "IST"])(
    "accepts the worldwide destination %s",
    (destination) => {
      expect(validate([], { ...ok, destination }, { now: NOW })).toMatchObject({
        ok: true,
        value: { destination },
      });
    },
  );

  it("rejects individual airports that a city code already covers (HND is part of TYO)", () => {
    expect(validate([], { ...ok, destination: "HND" }, { now: NOW })).toMatchObject({
      errors: ["unsupported_destination"],
    });
  });

  it("requires both places and rejects unsupported or identical ones", () => {
    expect(validate([], { ...ok, origin: "", destination: "" }, { now: NOW })).toEqual({
      ok: false,
      errors: ["origin_required", "destination_required"],
    });
    expect(validate([], { ...ok, origin: "NRT" }, { now: NOW })).toMatchObject({
      errors: ["unsupported_origin"],
    });
    expect(validate([], { ...ok, destination: "JNB" }, { now: NOW })).toMatchObject({
      errors: ["unsupported_destination"],
    });
    expect(validate([], { ...ok, destination: "TPE" }, { now: NOW })).toMatchObject({
      errors: expect.arrayContaining(["same_origin_destination"]),
    });
  });

  it.each(["", "0", "-5", "12.5", "abc", "1000001", "1e4"])("rejects target %j", (targetText) => {
    expect(validate([], { ...ok, targetText }, { now: NOW })).toMatchObject({
      errors: ["invalid_target_price"],
    });
  });

  it.each(["2026-11", "2027-12", "2026-13", "2026-1", "", ROLLING])(
    "rejects departure month %j for a new subscription",
    (month) => {
      expect(validate([], { ...ok, month }, { now: NOW })).toMatchObject({
        errors: ["invalid_departure_month"],
      });
    },
  );

  it("accepts every month from next month to 12 months out", () => {
    for (const month of ["2026-12", "2027-06", "2027-11"]) {
      expect(validate([], { ...ok, month }, { now: NOW })).toMatchObject({ ok: true });
    }
  });

  it("lets an edit keep the current value: legacy rolling, or an expired month", () => {
    const legacy = sub("TPE-TYO", { departure_month: null, plan_name: "tokyo" });
    expect(
      validate([legacy], { ...ok, month: ROLLING }, { editing: legacy, now: NOW }),
    ).toMatchObject({
      ok: true,
      value: { departure_month: null },
    });
    const expired = sub("TPE-TYO", { departure_month: "2026-09" });
    expect(
      validate([expired], { ...ok, month: "2026-09" }, { editing: expired, now: NOW }),
    ).toMatchObject({ ok: true });
    // A fixed-month row can't switch back to rolling, and can't move to another past month.
    expect(
      validate([expired], { ...ok, month: ROLLING }, { editing: expired, now: NOW }),
    ).toMatchObject({
      errors: ["invalid_departure_month"],
    });
    expect(
      validate([expired], { ...ok, month: "2026-08" }, { editing: expired, now: NOW }),
    ).toMatchObject({
      errors: ["invalid_departure_month"],
    });
  });

  it("allows other months of a route but no duplicate route and month (R-MM-1)", () => {
    const subs = [sub("TPE-TYO"), sub("KHH-OKA")];
    expect(validate(subs, { ...ok, month: "2027-03" }, { now: NOW })).toMatchObject({ ok: true });
    expect(validate(subs, ok, { now: NOW })).toMatchObject({
      errors: ["duplicate_subscription"],
    });
    expect(
      validate(subs, { ...ok, origin: "KHH", destination: "OKA" }, { editing: subs[0]!, now: NOW }),
    ).toMatchObject({ errors: ["duplicate_subscription"] });
    expect(
      validate(subs, { ...ok, targetText: "8000" }, { editing: subs[0]!, now: NOW }),
    ).toMatchObject({ ok: true });
    // Moving a month onto a sibling month of the same route is a duplicate.
    const siblings = [sub("TPE-TYO"), sub("TPE-TYO", { departure_month: "2027-01" })];
    expect(
      validate(siblings, { ...ok, month: "2027-01" }, { editing: siblings[0]!, now: NOW }),
    ).toMatchObject({ errors: ["duplicate_subscription"] });
  });

  it("lets a legacy rolling row coexist with fixed months of the same route", () => {
    const legacy = sub("TPE-TYO", { departure_month: null, plan_name: "tokyo" });
    expect(validate([legacy], ok, { now: NOW })).toMatchObject({ ok: true }); // 2026-12 = its month
    expect(
      validate([legacy], { ...ok, month: ROLLING }, { editing: legacy, now: NOW }),
    ).toMatchObject({ ok: true });
  });

  it("counts every record toward the limit: active, paused and expired", () => {
    const mixed = [
      ...ten().slice(0, 6),
      ...ten({ alerts_paused: true }).slice(6, 8),
      ...ten({ departure_month: "2026-01" }).slice(8, 10),
    ];
    expect(validate(mixed, { ...ok, destination: "DPS" }, { now: NOW })).toMatchObject({
      errors: ["limit_reached"],
    });
    expect(validate(mixed.slice(0, 9), { ...ok, destination: "DPS" }, { now: NOW })).toMatchObject({
      ok: true,
    });
    // Edits never change the number of records.
    expect(
      validate(mixed, { ...ok, targetText: "1" }, { editing: mixed[0]!, now: NOW }),
    ).toMatchObject({ ok: true });
  });
});

describe("reducer", () => {
  const value = { origin: "TPE", destination: "OKA", departure_month: "2027-01", target: 5000 };

  it("creates an active TWD row with the chosen month and no plan name", () => {
    const s = reducer(state([]), { type: "create", value, now: STAMP });
    expect(s.subs).toEqual([sub("TPE-OKA", { departure_month: "2027-01", target_price: 5000 })]);
    expect(s.subs[0]?.id).toBe("TPE-OKA_2027-01");
    expect(s.lastError).toBeNull();
  });

  it("refuses duplicates and an 11th record even if the UI is bypassed", () => {
    expect(
      reducer(state([sub("TPE-OKA", { departure_month: "2027-01" })]), {
        type: "create",
        value,
        now: STAMP,
      }).lastError?.code,
    ).toBe("duplicate_subscription");
    // Three months of one route are three records.
    let multi = state([]);
    for (const m of ["2027-01", "2027-02", "2027-03"]) {
      multi = reducer(multi, {
        type: "create",
        value: { ...value, departure_month: m },
        now: STAMP,
      });
    }
    expect(multi.subs.map((x) => x.id)).toEqual([
      "TPE-OKA_2027-01",
      "TPE-OKA_2027-02",
      "TPE-OKA_2027-03",
    ]);
    const full = reducer(state(ten({ alerts_paused: true })), {
      type: "create",
      value: { ...value, destination: "DPS" },
      now: STAMP,
    });
    expect(full.lastError?.code).toBe("limit_reached");
    expect(full.subs).toHaveLength(10);
  });

  it("pause and resume neither free nor consume quota", () => {
    let s = reducer(state(ten()), { type: "pause", id: "TPE-TYO_2026-12", now: STAMP });
    expect(s.subs).toHaveLength(10);
    expect(
      reducer(s, { type: "create", value: { ...value, destination: "DPS" }, now: STAMP }).lastError
        ?.code,
    ).toBe("limit_reached");
    s = reducer(s, { type: "resume", id: "TPE-TYO_2026-12", now: STAMP });
    expect(s.lastError).toBeNull();
    expect(statusOf(s.subs[0]!, NOW)).toBe("active");
  });

  it("deleting frees exactly one slot", () => {
    const s = reducer(state(ten()), { type: "delete", id: "TPE-TYO_2026-12" });
    expect(s.subs).toHaveLength(9);
    expect(
      reducer(s, { type: "create", value: { ...value, destination: "DPS" }, now: STAMP }).subs,
    ).toHaveLength(10);
    expect(reducer(state([]), { type: "delete", id: "TPE-TYO_2026-12" }).lastError?.code).toBe(
      "not_found",
    );
  });

  it("changes a route atomically: one row replaced, pause state kept, M1 plan name dropped", () => {
    const m1 = sub("TPE-TYO", {
      alerts_paused: true,
      plan_name: "tokyo",
      departure_month: null,
      created_at: "2026-10-30T00:00:00Z",
    });
    const s = reducer(state([m1, sub("KHH-OKA")]), {
      type: "edit",
      id: "TPE-TYO",
      value: { origin: "RMQ", destination: "SEL", departure_month: "2027-02", target: 7000 },
      now: STAMP,
    });
    expect(s.subs.map((x) => x.route)).toEqual(["RMQ-SEL", "KHH-OKA"]);
    expect(s.subs[0]).toMatchObject({
      alerts_paused: true,
      plan_name: null,
      departure_month: "2027-02",
      target_price: 7000,
      created_at: STAMP,
    });
  });

  it("keeps plan name, created_at and rolling mode when only the target changes", () => {
    const m1 = sub("TPE-TYO", {
      plan_name: "tokyo",
      departure_month: null,
      created_at: "2026-10-30T00:00:00Z",
    });
    const s = reducer(state([m1]), {
      type: "edit",
      id: "TPE-TYO",
      value: { origin: "TPE", destination: "TYO", departure_month: null, target: 8500 },
      now: STAMP,
    });
    expect(s.subs[0]).toMatchObject({
      plan_name: "tokyo",
      departure_month: null,
      created_at: "2026-10-30T00:00:00Z",
      target_price: 8500,
    });
  });

  it("reactivates an expired subscription when a future month is chosen", () => {
    const expired = sub("TPE-TYO", { departure_month: "2026-09" });
    expect(statusOf(expired, NOW)).toBe("expired");
    const s = reducer(state([expired]), {
      type: "edit",
      id: "TPE-TYO_2026-09",
      value: { origin: "TPE", destination: "TYO", departure_month: "2027-03", target: 9000 },
      now: STAMP,
    });
    expect(statusOf(s.subs[0]!, NOW)).toBe("active");
    expect(s.subs).toHaveLength(1);
    expect(s.subs[0]?.id).toBe("TPE-TYO_2027-03"); // the month change moved its ID
  });

  it("refuses a route change onto an existing route", () => {
    const s = reducer(state([sub("TPE-TYO"), sub("KHH-OKA")]), {
      type: "edit",
      id: "TPE-TYO_2026-12",
      value: { origin: "KHH", destination: "OKA", departure_month: "2026-12", target: 1 },
      now: STAMP,
    });
    expect(s.lastError?.code).toBe("duplicate_subscription");
    expect(s.subs.map((x) => x.route)).toEqual(["TPE-TYO", "KHH-OKA"]);
  });
});

describe("messages and demo data", () => {
  it("explains each error in Traditional Chinese", () => {
    expect(errorMessage("same_origin_destination")).toBe("出發地與目的地不能相同。");
    expect(errorMessage("limit_reached")).toContain(
      "最多 10 筆航線訂閱（已暫停與已過期的訂閱也算在內）",
    );
    expect(
      errorMessage("duplicate_subscription", {
        origin: "TPE",
        destination: "TYO",
        departure_month: "2027-01",
      }),
    ).toBe("你已經訂閱過「台北 → 東京」 2027 年 1 月出發的航線，同一航線同一月份只能訂閱一次。");
    expect(errorMessage("invalid_target_price")).toContain("1,000,000");
    expect(errorMessage("invalid_departure_month", { now: NOW })).toBe(
      "請選擇 2026 年 12 月 到 2027 年 11 月 之間的出發月份。",
    );
  });

  it("demo rows cover rolling, paused, expired and long-haul routes, with no personal data", () => {
    const demo = demoSubscriptions(NOW);
    expect(demo.map((d) => [d.route, statusOf(d, NOW), d.departure_month])).toEqual([
      ["TPE-TYO", "active", null],
      ["KHH-OKA", "paused", "2027-01"],
      ["RMQ-SEL", "expired", "2026-10"],
      ["TPE-LON", "active", "2027-02"],
      ["TPE-SYD", "active", "2026-12"],
    ]);
    expect(JSON.stringify(demo)).not.toContain("@");
  });
});
