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

export interface RouteSubscription {
  route: string; // "ORIG-DEST", the DynamoDB sort key
  origin: string;
  destination: string;
  target_price: number; // whole TWD
  currency: "TWD";
  alerts_paused: boolean;
  plan_name: string | null; // set only on the two M1 fixed-plan rows
  created_at: string;
  updated_at: string;
}

export type ErrorCode =
  | "origin_required"
  | "destination_required"
  | "unsupported_origin"
  | "unsupported_destination"
  | "same_origin_destination"
  | "invalid_target_price"
  | "duplicate_subscription"
  | "limit_reached"
  | "not_found";

export interface FieldInput {
  origin: string;
  destination: string;
  targetText: string;
}

const ORIGIN_CODES = new Set(ORIGINS.map((a) => a.code));
const DEST_CODES = new Set(DESTINATIONS.map((a) => a.code));

export function activeCount(subs: readonly RouteSubscription[]): number {
  return subs.filter((s) => !s.alerts_paused).length;
}

export function errorMessage(
  code: ErrorCode,
  ctx: { origin?: string; destination?: string; policy?: SubscriptionPolicy } = {},
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
    case "duplicate_subscription":
      return `你已經訂閱過「${routeLabel(ctx.origin ?? "", ctx.destination ?? "")}」這條航線。`;
    case "limit_reached":
      return `最多只能同時追蹤 ${policy.maxActive} 條航線，請先暫停或刪除其他訂閱。`;
    case "not_found":
      return "找不到這筆訂閱，可能已被刪除。";
  }
}

export interface Validated {
  origin: string;
  destination: string;
  target: number;
}

/**
 * Validates a create (editingRoute undefined) or an edit. Returns every field error found, in
 * display order, or the parsed values. Edits never change the active count, so only creates
 * check the limit.
 */
export function validate(
  subs: readonly RouteSubscription[],
  input: FieldInput,
  opts: { editingRoute?: string; policy?: SubscriptionPolicy } = {},
): { ok: true; value: Validated } | { ok: false; errors: ErrorCode[] } {
  const policy = opts.policy ?? POLICY;
  const errors: ErrorCode[] = [];
  const { origin, destination } = input;
  if (!origin) errors.push("origin_required");
  else if (!ORIGIN_CODES.has(origin)) errors.push("unsupported_origin");
  if (!destination) errors.push("destination_required");
  else if (!DEST_CODES.has(destination)) errors.push("unsupported_destination");
  if (origin && destination && origin === destination) errors.push("same_origin_destination");

  const target = parseTargetPrice(input.targetText);
  if (target === null || target < policy.minTarget || target > policy.maxTarget)
    errors.push("invalid_target_price");

  const route = routeKey(origin, destination);
  if (origin && destination && route !== opts.editingRoute && subs.some((s) => s.route === route)) {
    errors.push("duplicate_subscription");
  }
  if (opts.editingRoute === undefined && activeCount(subs) >= policy.maxActive)
    errors.push("limit_reached");

  return errors.length || target === null
    ? { ok: false, errors }
    : { ok: true, value: { origin, destination, target } };
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
      const { origin, destination, target } = action.value;
      const route = routeKey(origin, destination);
      if (find(route)) return fail("duplicate_subscription", route);
      if (activeCount(subs) >= policy.maxActive) return fail("limit_reached");
      const row: RouteSubscription = {
        route,
        origin,
        destination,
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
      const { origin, destination, target } = action.value;
      const route = routeKey(origin, destination);
      if (route !== old.route && find(route)) return fail("duplicate_subscription", route);
      // Route change = delete old + create new in one step (the API uses a DynamoDB transaction).
      // The new row keeps the pause state; plan_name only belongs to the original M1 routes.
      const changed = route !== old.route;
      const row: RouteSubscription = {
        ...old,
        route,
        origin,
        destination,
        target_price: target,
        updated_at: action.now,
        plan_name: changed ? null : old.plan_name,
        created_at: changed ? action.now : old.created_at,
      };
      return { subs: subs.map((s) => (s.route === old.route ? row : s)), lastError: null };
    }
    case "pause":
    case "resume": {
      const row = find(action.route);
      if (!row) return fail("not_found", action.route);
      const paused = action.type === "pause";
      if (row.alerts_paused === paused) return { subs, lastError: null };
      if (!paused && activeCount(subs) >= policy.maxActive)
        return fail("limit_reached", action.route);
      return {
        subs: subs.map((s) =>
          s.route === row.route ? { ...s, alerts_paused: paused, updated_at: action.now } : s,
        ),
        lastError: null,
      };
    }
    case "delete":
      if (!find(action.route)) return fail("not_found", action.route);
      return { subs: subs.filter((s) => s.route !== action.route), lastError: null };
    case "reset":
      return { subs: action.subs, lastError: null };
  }
}

/** Demo rows for the prototype only. Fake timestamps; no real user data. */
export function demoSubscriptions(): RouteSubscription[] {
  return [
    {
      route: "TPE-TYO",
      origin: "TPE",
      destination: "TYO",
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
      target_price: 4500,
      currency: "TWD",
      alerts_paused: true,
      plan_name: null,
      created_at: "2026-11-02T00:00:00Z",
      updated_at: "2026-11-03T00:00:00Z",
    },
  ];
}
