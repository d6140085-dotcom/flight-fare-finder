// What the subscription list says about fare checks. The prototype shows this with demo data;
// Phase 2 would supply it from the fare check (see docs/api/m1.4-subscriptions-api.md §3.4).
// Absence of a cached Travelpayouts fare never means "no flights" — the copy says so.
import { effectiveMonth, monthLabel } from "./months";
import { statusOf, type RouteSubscription } from "./subscriptions";

/** Result of the latest fare check for a subscription's (origin, destination, effective month). */
export type FareCheck =
  | { kind: "ok"; cheapest_twd: number; checked_at: string }
  | { kind: "no_data"; checked_at: string }
  | { kind: "error"; checked_at: string };

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
