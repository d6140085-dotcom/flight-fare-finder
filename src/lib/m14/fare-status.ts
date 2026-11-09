// What the subscription list says about fare checks. The prototype shows this with demo data;
// Phase 2 would supply it from the fare check (see docs/api/m1.4-subscriptions-api.md §3.4).
// Absence of a cached Travelpayouts fare never means "no flights" — the copy says so.
import { effectiveMonth, monthLabel } from "./months";
import { statusOf, type RouteSubscription } from "./subscriptions";

/**
 * Result of the latest fare check for a subscription's (origin, destination, effective month).
 * Shape of `fare_check` in the subscriptions API (contract §3.4), produced by the Phase 2A fare
 * check (`aws/lambdas/flight-fare-check/farecheck/status.py`). The itinerary fields are optional
 * so older records and the prototype's demo data stay valid; Phase 3 shows search links only when
 * both dates are present (docs/m1.4-multi-platform-search-links.md §2.2).
 */
export type FareCheck =
  | {
      kind: "ok";
      cheapest_twd: number;
      checked_at: string;
      /** Always "TWD": the fare check never accepts another currency. */
      currency?: "TWD";
      /** YYYY-MM-DD, local date of the outbound flight. */
      depart_date?: string;
      /** YYYY-MM-DD, local date of the return flight (fares are always round trips). */
      return_date?: string;
      /** Actual airports when Travelpayouts reports them (v3), e.g. TSA for a TPE search. */
      origin_airport?: string | null;
      destination_airport?: string | null;
    }
  | { kind: "no_data"; checked_at: string }
  | { kind: "error"; checked_at: string };

/** True when an ok result carries a complete round-trip itinerary (needed for search links). */
export const hasItinerary = (check: FareCheck | null): boolean =>
  check?.kind === "ok" && Boolean(check.depart_date && check.return_date);

export type FareDisplayKind = "expired" | "paused" | "ok" | "no_data" | "error" | "pending";

export interface FareDisplay {
  kind: FareDisplayKind;
  text: string;
}

const twd = new Intl.NumberFormat("zh-TW");

/** Subscription state wins over fare data: expired and paused rows are not being checked. */
export function fareDisplay(
  sub: RouteSubscription,
  now: Date,
  check: FareCheck | null,
): FareDisplay {
  const status = statusOf(sub, now);
  if (status === "expired") {
    return { kind: "expired", text: "已過期：出發月份已過，不再查詢票價。" };
  }
  if (status === "paused") {
    return { kind: "paused", text: "已暫停：暫時不查詢票價，也不寄送通知。" };
  }
  const month = monthLabel(effectiveMonth(sub.departure_month, now));
  if (!check) return { kind: "pending", text: `尚未查詢 ${month} 的票價，會在下次排程查詢。` };
  switch (check.kind) {
    case "ok":
      return {
        kind: "ok",
        text: `最近查到 ${month} 出發最低 NT$${twd.format(check.cheapest_twd)}。`,
      };
    case "no_data":
      return {
        kind: "no_data",
        text: `目前沒有 ${month} 的近期票價資料（票價資料來源尚無快取，不代表沒有航班），會持續查詢。`,
      };
    case "error":
      return { kind: "error", text: "票價查詢暫時失敗（服務錯誤），會在下次排程自動重試。" };
  }
}

/** Fare checks are per (route, effective month), so a month or route change starts fresh. */
export const fareCheckKey = (sub: RouteSubscription, now: Date) =>
  `${sub.route}#${effectiveMonth(sub.departure_month, now)}`;

/** Demo fare-check results for the prototype's demo rows (one of each kind). */
export function demoFareChecks(
  subs: readonly RouteSubscription[],
  now: Date,
): Map<string, FareCheck> {
  const at = "2026-11-05T02:30:00Z";
  const demo: Record<string, FareCheck> = {
    "TPE-TYO": { kind: "ok", cheapest_twd: 9820, checked_at: at },
    "TPE-LON": { kind: "no_data", checked_at: at },
    "TPE-SYD": { kind: "error", checked_at: at },
  };
  const out = new Map<string, FareCheck>();
  for (const sub of subs) {
    const check = demo[sub.route];
    if (check) out.set(fareCheckKey(sub, now), check);
  }
  return out;
}
