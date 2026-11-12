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
import { subscriptionId } from "./subscription-id";

export interface RouteSubscription {
  /**
   * Public subscription ID (R-MM-1): "KHH-TYO" for the legacy rolling row, "KHH-TYO_2027-01"
   * for a fixed month. One subscription per (route pair, fixed month), plus at most one rolling
   * row per pair. Used in API paths; see subscription-id.ts.
   */
  id: string;
  route: string; // "ORIG-DEST", the route pair (several subscriptions can share it)
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
  ctx: {
    origin?: string;
    destination?: string;
    /** For duplicate_subscription: the fixed month, or null for the rolling row. */
    departure_month?: string | null;
    now?: Date;
    policy?: SubscriptionPolicy;
  } = {},
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
    case "duplicate_subscription": {
      const when = ctx.departure_month ? ` ${monthLabel(ctx.departure_month)}` : "（每月滾動）";
      return `你已經訂閱過「${routeLabel(ctx.origin ?? "", ctx.destination ?? "")}」${when}出發的航線，同一航線同一月份只能訂閱一次。`;
    }
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

  // One subscription per (route, fixed month) and one rolling row per route (R-MM-1).
  if (origin && destination && (departure_month !== null || month === ROLLING)) {
    const id = subscriptionId(origin, destination, departure_month);
    if (id !== editing?.id && subs.some((s) => s.id === id)) errors.push("duplicate_subscription");
  }
  if (editing === undefined && subs.length >= policy.maxSubscriptions) errors.push("limit_reached");

  return errors.length || target === null
    ? { ok: false, errors }
    : { ok: true, value: { origin, destination, departure_month, target } };
}

/** Actions address a subscription by its public ID (`id`), never by the route pair. */
export type Action =
  | { type: "create"; value: Validated; now: string }
  | { type: "edit"; id: string; value: Validated; now: string }
  | { type: "pause"; id: string; now: string }
  | { type: "resume"; id: string; now: string }
  | { type: "delete"; id: string }
  | { type: "reset"; subs: RouteSubscription[] };

export interface State {
  subs: RouteSubscription[];
  lastError: { code: ErrorCode; id?: string } | null;
}

/** Applies one action with the same guarantees the API will give (see the API spec). */
export function reducer(state: State, action: Action, policy: SubscriptionPolicy = POLICY): State {
  const { subs } = state;
  const fail = (code: ErrorCode, id?: string): State => ({
    subs,
    lastError: id ? { code, id } : { code },
  });
  const find = (id: string) => subs.find((s) => s.id === id);

  switch (action.type) {
    case "create": {
      const { origin, destination, departure_month, target } = action.value;
      const id = subscriptionId(origin, destination, departure_month);
      if (find(id)) return fail("duplicate_subscription", id);
      // Strict total limit: every record counts, whatever its status (each month counts).
      if (subs.length >= policy.maxSubscriptions) return fail("limit_reached");
      const row: RouteSubscription = {
        id,
        route: routeKey(origin, destination),
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
      const old = find(action.id);
      if (!old) return fail("not_found", action.id);
      const { origin, destination, departure_month, target } = action.value;
      const id = subscriptionId(origin, destination, departure_month);
      if (id !== old.id && find(id)) return fail("duplicate_subscription", id);
      // A route or month change moves the record to another ID (delete old + create new in one
      // step; the API uses a DynamoDB transaction). The record count doesn't change. Pause state
      // is kept; plan_name only belongs to the rolling M1 rows; a month-only change keeps
      // created_at. Choosing a future month reactivates an expired subscription.
      const route = routeKey(origin, destination);
      const routeChanged = route !== old.route;
      const row: RouteSubscription = {
        ...old,
        id,
        route,
        origin,
        destination,
        departure_month,
        target_price: target,
        updated_at: action.now,
        plan_name: id === old.id ? old.plan_name : null,
        created_at: routeChanged ? action.now : old.created_at,
      };
      return { subs: subs.map((s) => (s.id === old.id ? row : s)), lastError: null };
    }
    case "pause":
    case "resume": {
      // Neither frees nor consumes quota.
      const row = find(action.id);
      if (!row) return fail("not_found", action.id);
      const paused = action.type === "pause";
      if (row.alerts_paused === paused) return { subs, lastError: null };
      return {
        subs: subs.map((s) =>
          s.id === row.id ? { ...s, alerts_paused: paused, updated_at: action.now } : s,
        ),
        lastError: null,
      };
    }
    case "delete":
      // The only action that frees a slot.
      if (!find(action.id)) return fail("not_found", action.id);
      return { subs: subs.filter((s) => s.id !== action.id), lastError: null };
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
      id: "TPE-TYO",
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
      id: `KHH-OKA_${addMonths(thisMonth, 2)}`,
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
      id: `RMQ-SEL_${addMonths(thisMonth, -1)}`,
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
    {
      id: `TPE-LON_${addMonths(thisMonth, 3)}`,
      route: "TPE-LON",
      origin: "TPE",
      destination: "LON",
      departure_month: addMonths(thisMonth, 3),
      target_price: 28000,
      currency: "TWD",
      alerts_paused: false,
      plan_name: null,
      created_at: "2026-11-04T00:00:00Z",
      updated_at: "2026-11-04T00:00:00Z",
    },
    {
      id: `TPE-SYD_${addMonths(thisMonth, 1)}`,
      route: "TPE-SYD",
      origin: "TPE",
      destination: "SYD",
      departure_month: addMonths(thisMonth, 1),
      target_price: 18000,
      currency: "TWD",
      alerts_paused: false,
      plan_name: null,
      created_at: "2026-11-04T00:00:00Z",
      updated_at: "2026-11-04T00:00:00Z",
    },
  ];
}
