import { describe, expect, it } from "vitest";

import apiViews from "../../../aws/tests/fixtures/fare_check_api_view.json";
import {
  demoFareChecks,
  fareCheckKey,
  fareDisplay,
  hasItinerary,
  type FareCheck,
} from "./fare-status";
import { demoSubscriptions, type RouteSubscription } from "./subscriptions";

const NOW = new Date("2026-11-05T03:00:00Z");
const at = "2026-11-05T02:30:00Z";
const base: RouteSubscription = {
  route: "TPE-LON",
  origin: "TPE",
  destination: "LON",
  departure_month: "2027-02",
  target_price: 28000,
  currency: "TWD",
  alerts_paused: false,
  plan_name: null,
  created_at: at,
  updated_at: at,
};

describe("fare status display", () => {
  it("distinguishes no data, API error, paused, expired, a fare and not yet checked", () => {
    expect(fareDisplay(base, NOW, { kind: "no_data", checked_at: at })).toEqual({
      kind: "no_data",
      text: "目前沒有 2027 年 2 月 的近期票價資料（票價資料來源尚無快取，不代表沒有航班），會持續查詢。",
      checkedAt: at,
    });
    expect(fareDisplay(base, NOW, { kind: "error", checked_at: at }).kind).toBe("error");
    expect(fareDisplay(base, NOW, { kind: "error", checked_at: at }).text).toContain("服務錯誤");
    expect(fareDisplay(base, NOW, { kind: "ok", cheapest_twd: 27500, checked_at: at }).text).toBe(
      "參考票價（來源：Aviasales）：約 NT$27,500（2027 年 2 月 出發）",
    );
    expect(fareDisplay(base, NOW, null).kind).toBe("pending");
    // Subscription state wins over any fare data.
    expect(
      fareDisplay({ ...base, alerts_paused: true }, NOW, { kind: "error", checked_at: at }).kind,
    ).toBe("paused");
    expect(
      fareDisplay({ ...base, departure_month: "2026-10" }, NOW, {
        kind: "ok",
        cheapest_twd: 1,
        checked_at: at,
      }).kind,
    ).toBe("expired");
  });

  it("never says a route has no flights", () => {
    for (const kind of ["no_data", "error"] as const) {
      expect(fareDisplay(base, NOW, { kind, checked_at: at }).text).not.toMatch(
        /沒有航班。|無航班/,
      );
    }
  });

  it("keys checks by route and effective month, so a month change starts fresh", () => {
    expect(fareCheckKey(base, NOW)).toBe("TPE-LON#2027-02");
    expect(fareCheckKey({ ...base, departure_month: null }, NOW)).toBe("TPE-LON#2026-12");
    const checks = demoFareChecks(demoSubscriptions(NOW), NOW);
    expect([...checks.keys()].sort()).toEqual([
      "TPE-LON#2027-02",
      "TPE-SYD#2026-12",
      "TPE-TYO#2026-12",
    ]);
  });
});

describe("fare_check from the Phase 2A fare check", () => {
  // Same fixture as aws/tests/test_m14_fare_check.py, so the Python output and this type agree.
  const ok = apiViews.ok as FareCheck;
  const noData = apiViews.no_data as FareCheck;
  const error = apiViews.error as FareCheck;

  it("renders the API shape for every kind", () => {
    const sub = { ...base, route: "TPE-TYO", destination: "TYO", departure_month: "2026-12" };
    expect(fareDisplay(sub, NOW, ok).text).toBe(
      "參考票價（來源：Aviasales）：約 NT$8,062（2026 年 12 月 出發）",
    );
    expect(fareDisplay(sub, NOW, noData).kind).toBe("no_data");
    expect(fareDisplay(sub, NOW, error).kind).toBe("error");
  });

  it("keeps the itinerary needed for future search links", () => {
    expect(ok).toMatchObject({
      currency: "TWD",
      depart_date: "2026-12-11",
      return_date: "2026-12-18",
      origin_airport: "TSA",
      destination_airport: "HND",
    });
    expect(hasItinerary(ok)).toBe(true);
    expect(hasItinerary(noData)).toBe(false);
    expect(hasItinerary(null)).toBe(false);
    expect(hasItinerary({ kind: "ok", cheapest_twd: 9820, checked_at: at })).toBe(false);
  });
});
