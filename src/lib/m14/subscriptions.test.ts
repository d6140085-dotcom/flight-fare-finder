import { describe, expect, it } from "vitest";

import { MAX_TARGET_TWD } from "@/lib/flight-api";
import { DESTINATIONS, ORIGINS, POLICY, routeLabel } from "./catalog";
import {
  activeCount,
  demoSubscriptions,
  errorMessage,
  reducer,
  validate,
  type RouteSubscription,
  type State,
} from "./subscriptions";

const NOW = "2026-11-05T00:00:00Z";
const sub = (route: string, paused = false): RouteSubscription => {
  const [origin = "", destination = ""] = route.split("-");
  return {
    route,
    origin,
    destination,
    target_price: 9000,
    currency: "TWD",
    alerts_paused: paused,
    plan_name: null,
    created_at: NOW,
    updated_at: NOW,
  };
};
const state = (subs: RouteSubscription[]): State => ({ subs, lastError: null });
// Ten distinct active routes from TPE.
const ten = () => DESTINATIONS.slice(0, 10).map((d) => sub(`TPE-${d.code}`));

describe("catalog", () => {
  it("offers the three origins and 25 destinations from airports.json", () => {
    expect(ORIGINS.map((a) => a.code)).toEqual(["TPE", "KHH", "RMQ"]);
    expect(DESTINATIONS).toHaveLength(25);
    expect(routeLabel("TPE", "TYO")).toBe("台北 → 東京");
  });

  it("uses the same policy as the existing M1 price validation", () => {
    expect(POLICY).toEqual({ maxActive: 10, minTarget: 1, maxTarget: 1_000_000 });
    expect(POLICY.maxTarget).toBe(MAX_TARGET_TWD);
  });
});

describe("validate", () => {
  const ok = { origin: "TPE", destination: "TYO", targetText: "9,000" };

  it("accepts a valid new route and parses the target like M1 does", () => {
    expect(validate([], ok)).toEqual({
      ok: true,
      value: { origin: "TPE", destination: "TYO", target: 9000 },
    });
  });

  it("requires both places and rejects unsupported or identical ones", () => {
    expect(validate([], { ...ok, origin: "", destination: "" })).toEqual({
      ok: false,
      errors: ["origin_required", "destination_required"],
    });
    expect(validate([], { ...ok, origin: "NRT" })).toMatchObject({
      errors: ["unsupported_origin"],
    });
    expect(validate([], { ...ok, destination: "LAX" })).toMatchObject({
      errors: ["unsupported_destination"],
    });
    expect(validate([], { ...ok, destination: "TPE" })).toMatchObject({
      errors: expect.arrayContaining(["same_origin_destination"]),
    });
  });

  it.each(["", "0", "-5", "12.5", "abc", "1000001", "1e4"])("rejects target %j", (targetText) => {
    expect(validate([], { ...ok, targetText })).toMatchObject({ errors: ["invalid_target_price"] });
  });

  it("prevents duplicate routes on create and on edit, but allows keeping the same route", () => {
    const subs = [sub("TPE-TYO"), sub("KHH-OKA")];
    expect(validate(subs, ok)).toMatchObject({ errors: ["duplicate_subscription"] });
    expect(
      validate(subs, { ...ok, origin: "KHH", destination: "OKA" }, { editingRoute: "TPE-TYO" }),
    ).toMatchObject({
      errors: ["duplicate_subscription"],
    });
    expect(
      validate(subs, { ...ok, targetText: "8000" }, { editingRoute: "TPE-TYO" }),
    ).toMatchObject({ ok: true });
  });

  it("enforces the active limit on create; paused rows don't count; edits are exempt", () => {
    expect(validate(ten(), { ...ok, destination: "DPS" })).toMatchObject({
      errors: ["limit_reached"],
    });
    const withPaused = [...ten().slice(0, 9), sub("TPE-DPS", true)];
    expect(validate(withPaused, { ...ok, destination: "BKI" })).toMatchObject({ ok: true });
    expect(
      validate(ten(), { ...ok, destination: "DPS" }, { editingRoute: "TPE-TYO" }),
    ).toMatchObject({ ok: true });
  });
});

describe("reducer", () => {
  const value = { origin: "TPE", destination: "OKA", target: 5000 };

  it("creates an active TWD row with no plan name", () => {
    const s = reducer(state([]), { type: "create", value, now: NOW });
    expect(s.subs).toEqual([{ ...sub("TPE-OKA"), target_price: 5000 }]);
    expect(s.lastError).toBeNull();
  });

  it("refuses duplicates and an 11th active subscription even if the UI is bypassed", () => {
    expect(
      reducer(state([sub("TPE-OKA")]), { type: "create", value, now: NOW }).lastError?.code,
    ).toBe("duplicate_subscription");
    const full = reducer(state(ten()), {
      type: "create",
      value: { ...value, destination: "DPS" },
      now: NOW,
    });
    expect(full.lastError?.code).toBe("limit_reached");
    expect(full.subs).toHaveLength(10);
  });

  it("changes a route atomically: one row replaced, pause state kept, M1 plan name dropped", () => {
    const m1 = { ...sub("TPE-TYO", true), plan_name: "tokyo", created_at: "2026-10-30T00:00:00Z" };
    const s = reducer(state([m1, sub("KHH-OKA")]), {
      type: "edit",
      route: "TPE-TYO",
      value: { origin: "RMQ", destination: "SEL", target: 7000 },
      now: NOW,
    });
    expect(s.subs.map((x) => x.route)).toEqual(["RMQ-SEL", "KHH-OKA"]);
    expect(s.subs[0]).toMatchObject({
      alerts_paused: true,
      plan_name: null,
      target_price: 7000,
      created_at: NOW,
    });
  });

  it("keeps plan name and created_at when only the target changes", () => {
    const m1 = { ...sub("TPE-TYO"), plan_name: "tokyo", created_at: "2026-10-30T00:00:00Z" };
    const s = reducer(state([m1]), {
      type: "edit",
      route: "TPE-TYO",
      value: { origin: "TPE", destination: "TYO", target: 8500 },
      now: NOW,
    });
    expect(s.subs[0]).toMatchObject({
      plan_name: "tokyo",
      created_at: "2026-10-30T00:00:00Z",
      target_price: 8500,
    });
  });

  it("refuses a route change onto an existing route", () => {
    const s = reducer(state([sub("TPE-TYO"), sub("KHH-OKA")]), {
      type: "edit",
      route: "TPE-TYO",
      value: { origin: "KHH", destination: "OKA", target: 1 },
      now: NOW,
    });
    expect(s.lastError?.code).toBe("duplicate_subscription");
    expect(s.subs.map((x) => x.route)).toEqual(["TPE-TYO", "KHH-OKA"]);
  });

  it("pauses and resumes; resume re-checks the limit", () => {
    let s = reducer(state([sub("TPE-TYO")]), { type: "pause", route: "TPE-TYO", now: NOW });
    expect(activeCount(s.subs)).toBe(0);
    s = reducer(s, { type: "resume", route: "TPE-TYO", now: NOW });
    expect(activeCount(s.subs)).toBe(1);
    const blocked = reducer(state([...ten(), sub("TPE-DPS", true)]), {
      type: "resume",
      route: "TPE-DPS",
      now: NOW,
    });
    expect(blocked.lastError?.code).toBe("limit_reached");
    expect(blocked.subs.at(-1)?.alerts_paused).toBe(true);
  });

  it("deletes one row and reports missing rows", () => {
    expect(reducer(state([sub("TPE-TYO")]), { type: "delete", route: "TPE-TYO" }).subs).toEqual([]);
    expect(reducer(state([]), { type: "delete", route: "TPE-TYO" }).lastError?.code).toBe(
      "not_found",
    );
  });
});

describe("messages and demo data", () => {
  it("explains each error in Traditional Chinese", () => {
    expect(errorMessage("same_origin_destination")).toBe("出發地與目的地不能相同。");
    expect(errorMessage("limit_reached")).toContain("10 條航線");
    expect(errorMessage("duplicate_subscription", { origin: "TPE", destination: "TYO" })).toContain(
      "台北 → 東京",
    );
    expect(errorMessage("invalid_target_price")).toContain("1,000,000");
  });

  it("demo rows are valid routes with no real personal data", () => {
    const demo = demoSubscriptions();
    expect(demo.map((d) => d.route)).toEqual(["TPE-TYO", "KHH-OKA"]);
    expect(JSON.stringify(demo)).not.toContain("@");
  });
});
