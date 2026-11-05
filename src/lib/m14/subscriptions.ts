// M1.4 prototype: subscription rules and local state.
// Shapes and error codes mirror docs/api/m1.4-subscriptions-api.md so Phase 1B can replace this
// in-memory reducer with real API calls without changing the UI. Nothing here touches the network.
import { parseTargetPrice } from "@/lib/flight-api";
import {
  DESTINATIONS,
  ORIGINS,
  POLICY,
  routeKey,
  routeLabel,
  type SubscriptionPolicy,
} from "./catalog";
import {
  MONTH_RE,
  addMonths,
  currentMonth,
  isExpiredMonth,
  monthLabel,
  selectableMonths,
} from "./months";

export interface RouteSubscription {
  route: string; // "ORIG-DEST", the DynamoDB sort key; one subscription per route pair
  origin: string;
  destination: string;
  /** Fixed departure month "YYYY-MM", or null for a legacy M1 rolling next-month subscription. */
  departure_month: string | null;
  target_price: number; // whole TWD
  currency: "TWD";
  alerts_paused: boolean;
  plan_name: string | null; // set only on the two M1 fixed-plan rows
  created_at: string;
  updated_at: string;
}

export type Status = "active" | "paused" | "expired";

/** Expiry wins over pause: an expired subscription gets no fare checks either way. */
export function statusOf(sub: RouteSubscription, now: Date, policy = POLICY): Status {
  if (isExpiredMonth(sub.departure_month, now, policy)) return "expired";
  return sub.alerts_paused ? "paused" : "active";
}

/** The form value for "keep the legacy rolling next-month behaviour". */
export const ROLLING = "rolling";

export type ErrorCode =
  | "origin_required"
  | "destination_required"
  | "unsupported_origin"
  | "unsupported_destination"
  | "same_origin_destination"
  | "invalid_target_price"
  | "invalid_departure_month"
  | "duplicate_subscription"
  | "limit_reached"
  | "not_found";

export interface FieldInput {
  origin: string;
  destination: string;
  /** "YYYY-MM", or ROLLING (only allowed when editing a row that is already rolling). */
  month: string;
  targetText: string;
}

const ORIGIN_CODES = new Set(ORIGINS.map((a) => a.code));
const DEST_CODES = new Set(DESTINATIONS.map((a) => a.code));

export function errorMessage(
  code: ErrorCode,
  ctx: { origin?: string; destination?: string; now?: Date; policy?: SubscriptionPolicy } = {},
): string {
  const policy = ctx.policy ?? POLICY;
  const fmt = new Intl.NumberFormat("zh-TW");
  switch (code) {
    case "origin_required":
      return "請選擇出發地。";
    case "destination_required":
      return "請選擇目的地。";
    case "unsupported_origin":
      return "目前不支援這個出發地。";
    case "unsupported_destination":
      return "目前不支援這個目的地。";
    case "same_origin_destination":
      return "出發地與目的地不能相同。";
    case "invalid_target_price":
      return `目標價請輸入 ${fmt.format(policy.minTarget)} 到 ${fmt.format(policy.maxTarget)} 之間的整數（新台幣）。`;
    case "invalid_departure_month": {
      const months = selectableMonths(ctx.now ?? new Date(), policy);
      return `請選擇 ${monthLabel(months[0] ?? "")} 到 ${monthLabel(months.at(-1) ?? "")} 之間的出發月份。`;
    }
    case "duplicate_subscription":
      return `你已經訂閱過「${routeLabel(ctx.origin ?? "", ctx.destination ?? "")}」這條航線（每條航線只能訂閱一個月份）。`;
    case "limit_reached":
      return `每位使用者最多 ${policy.maxSubscriptions} 筆航線訂閱（已暫停與已過期的訂閱也算在內），請先刪除不需要的訂閱。`;
    case "not_found":
      return "找不到這筆訂閱，可能已被刪除。";
  }
}

export interface Validated {
  origin: string;
  destination: string;
  departure_month: string | null;
  target: number;
}

/**
 * Validates a create (no `editing`) or an edit. Returns every field error found, in display
 * order, or the parsed values. Only creates can hit the limit: edits, pauses and resumes never
 * change the number of records.
 */
export function validate(
  subs: readonly RouteSubscription[],
  input: FieldInput,
  opts: { editing?: RouteSubscription; now: Date; policy?: SubscriptionPolicy },
): { ok: true; value: Validated } | { ok: false; errors: ErrorCode[] } {
  const policy = opts.policy ?? POLICY;
  const { editing, now } = opts;
  const errors: ErrorCode[] = [];
  const { origin, destination, month } = input;
  if (!origin) errors.push("origin_required");
  else if (!ORIGIN_CODES.has(origin)) errors.push("unsupported_origin");
  if (!destination) errors.push("destination_required");
  else if (!DEST_CODES.has(destination)) errors.push("unsupported_destination");
  if (origin && destination && origin === destination) errors.push("same_origin_destination");

  // Months: a new choice must be in the selectable window. Keeping a row's current value is
  // always allowed (including an expired month, and the legacy rolling mode).
  let departure_month: string | null = null;
  if (month === ROLLING) {
    if (editing?.departure_month !== null || editing === undefined)
      errors.push("invalid_departure_month");
  } else if (!MONTH_RE.test(month)) {
    errors.push("invalid_departure_month");
  } else if (month !== editing?.departure_month && !selectableMonths(now, policy).includes(month)) {
    errors.push("invalid_departure_month");
  } else {
    departure_month = month;
  }

  const target = parseTargetPrice(input.targetText);
  if (target === null || target < policy.minTarget || target > policy.maxTarget) {
    errors.push("invalid_target_price");
  }

  const route = routeKey(origin, destination);
  if (origin && destination && route !== editing?.route && subs.some((s) => s.route === route)) {
    errors.push("duplicate_subscription");
  }
  if (editing === undefined && subs.length >= policy.maxSubscriptions) errors.push("limit_reached");

  return errors.length || target === null
    ? { ok: false, errors }
    : { ok: true, value: { origin, destination, departure_month, target } };
}

export type Action =
  | { type: "create"; value: Validated; now: string }
  | { type: "edit"; route: string; value: Validated; now: string }
  | { type: "pause"; route: string; now: string }
  | { type: "resume"; route: string; now: string }
  | { type: "delete"; route: string }
  | { type: "reset"; subs: RouteSubscription[] };

export interface State {
  subs: RouteSubscription[];
  lastError: { code: ErrorCode; route?: string } | null;
}

/** Applies one action with the same guarantees the API will give (see the API spec). */
export function reducer(state: State, action: Action, policy: SubscriptionPolicy = POLICY): State {
  const { subs } = state;
  const fail = (code: ErrorCode, route?: string): State => ({
    subs,
    lastError: route ? { code, route } : { code },
  });
  const find = (route: string) => subs.find((s) => s.route === route);

  switch (action.type) {
    case "create": {
      const { origin, destination, departure_month, target } = action.value;
      const route = routeKey(origin, destination);
      if (find(route)) return fail("duplicate_subscription", route);
      // Strict total limit: every record counts, whatever its status.
      if (subs.length >= policy.maxSubscriptions) return fail("limit_reached");
      const row: RouteSubscription = {
        route,
        origin,
        destination,
        departure_month,
        target_price: target,
        currency: "TWD",
        alerts_paused: false,
        plan_name: null,
        created_at: action.now,
        updated_at: action.now,
      };
      return { subs: [...subs, row], lastError: null };
    }
    case "edit": {
      const old = find(action.route);
      if (!old) return fail("not_found", action.route);
      const { origin, destination, departure_month, target } = action.value;
      const route = routeKey(origin, destination);
      if (route !== old.route && find(route)) return fail("duplicate_subscription", route);
      // Route change = delete old + create new in one step (the API uses a DynamoDB transaction).
      // The record count doesn't change. Pause state is kept; plan_name only belongs to the
      // original M1 routes. Choosing a future month reactivates an expired subscription.
      const changed = route !== old.route;
      const row: RouteSubscription = {
        ...old,
        route,
        origin,
        destination,
        departure_month,
        target_price: target,
        updated_at: action.now,
        plan_name: changed ? null : old.plan_name,
        created_at: changed ? action.now : old.created_at,
      };
      return { subs: subs.map((s) => (s.route === old.route ? row : s)), lastError: null };
    }
    case "pause":
    case "resume": {
      // Neither frees nor consumes quota.
      const row = find(action.route);
      if (!row) return fail("not_found", action.route);
      const paused = action.type === "pause";
      if (row.alerts_paused === paused) return { subs, lastError: null };
      return {
        subs: subs.map((s) =>
          s.route === row.route ? { ...s, alerts_paused: paused, updated_at: action.now } : s,
        ),
        lastError: null,
      };
    }
    case "delete":
      // The only action that frees a slot.
      if (!find(action.route)) return fail("not_found", action.route);
      return { subs: subs.filter((s) => s.route !== action.route), lastError: null };
    case "reset":
      return { subs: action.subs, lastError: null };
  }
}

/** Demo rows for the prototype only: a legacy rolling M1 row, a paused fixed-month row and an
 * expired one. Fake timestamps; no real user data. */
export function demoSubscriptions(now: Date, policy = POLICY): RouteSubscription[] {
  const thisMonth = currentMonth(now, policy.timeZone);
  return [
    {
      route: "TPE-TYO",
      origin: "TPE",
      destination: "TYO",
      departure_month: null,
      target_price: 9000,
      currency: "TWD",
      alerts_paused: false,
      plan_name: "tokyo",
      created_at: "2026-11-01T00:00:00Z",
      updated_at: "2026-11-01T00:00:00Z",
    },
    {
      route: "KHH-OKA",
      origin: "KHH",
      destination: "OKA",
      departure_month: addMonths(thisMonth, 2),
      target_price: 4500,
      currency: "TWD",
      alerts_paused: true,
      plan_name: null,
      created_at: "2026-11-02T00:00:00Z",
      updated_at: "2026-11-03T00:00:00Z",
    },
    {
      route: "RMQ-SEL",
      origin: "RMQ",
      destination: "SEL",
      departure_month: addMonths(thisMonth, -1),
      target_price: 6000,
      currency: "TWD",
      alerts_paused: false,
      plan_name: null,
      created_at: "2026-09-20T00:00:00Z",
      updated_at: "2026-09-20T00:00:00Z",
    },
  ];
}
