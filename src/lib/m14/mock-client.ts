// In-memory implementation of SubscriptionsClient for Phase 3A (development and preview only).
// It follows the subscriptions API contract (docs/api/m1.4-subscriptions-api.md): the same
// validation order and error codes, the strict 10-record limit, one subscription per (route,
// fixed month) plus one rolling row per route (R-MM-1), public IDs in update/remove, atomic
// route and month changes, optimistic concurrency on random row versions (§4.5), derived status/effective month.
// It never calls fetch, Supabase or AWS, and every row and fare is fake demo data.
import { POLICY, airport } from "./catalog";
import { parseSubscriptionId, subscriptionId } from "./subscription-id";
import { mockDataAllowed } from "./routes";
import type { FareCheck } from "./fare-status";
import { addMonths, currentMonth, effectiveMonth, selectableMonths } from "./months";
import {
  ROLLING,
  reducer,
  statusOf,
  validate,
  type ErrorCode,
  type RouteSubscription,
  type Validated,
} from "./subscriptions";
import {
  ApiError,
  parseFareCheck,
  type ApiErrorCode,
  type CreateRequest,
  type ListResponse,
  type PatchRequest,
  type SubscriptionDto,
  type SubscriptionsClient,
} from "./api";

export type MockScenario =
  | "demo" // legacy rolling row, active/paused/expired rows, one of each fare state
  | "empty" // no subscriptions yet
  | "full" // 10 records: the limit is reached
  | "multi_month" // several months of one route, plus a rolling/fixed overlap
  | "list_error" // GET /subscriptions fails (internal_error)
  | "unauthorized"; // GET /subscriptions answers 401

export const MOCK_SCENARIOS: readonly { id: MockScenario; label: string }[] = [
  { id: "demo", label: "示範資料" },
  { id: "empty", label: "沒有訂閱" },
  { id: "multi_month", label: "同航線多個月份" },
  { id: "full", label: "已達 10 筆上限" },
  { id: "list_error", label: "載入失敗" },
  { id: "unauthorized", label: "登入過期" },
];

export interface MockOptions {
  now?: () => Date;
  scenario?: MockScenario;
  /** Simulated network latency, so loading states are visible (0 in tests). */
  latencyMs?: number;
}

const HTTP_STATUS: Partial<Record<ApiErrorCode, number>> = {
  duplicate_subscription: 409,
  limit_reached: 409,
  conflict: 409,
  version_required: 428,
  not_found: 404,
  unauthorized: 401,
  internal_error: 500,
};

const fail = (code: ApiErrorCode, details: Record<string, unknown> = {}): never => {
  throw new ApiError(code, HTTP_STATUS[code] ?? 400, details);
};

const CHECKED_AT = "2026-11-11T02:30:00Z";

const VERSION_RE = /^(?:[A-Za-z0-9_-]{22}|legacy-[0-9a-f]{32})$/;

/** 128 random bits, base64url (22 chars) — the same shape as the API's version tokens. */
function randomVersion(): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

/**
 * Mock fare checks for the demo rows, keyed by route only and valid for the row's current
 * effective month. A changed route or month starts as "not checked yet", as the real API does.
 */
function demoFare(route: string, month: string): FareCheck | null {
  const [y, m] = month.split("-");
  const d = (day: number) => `${y}-${m}-${String(day).padStart(2, "0")}`;
  switch (route) {
    case "TPE-TYO":
      return {
        kind: "ok",
        cheapest_twd: 8062,
        checked_at: CHECKED_AT,
        currency: "TWD",
        depart_date: d(11),
        return_date: d(18),
        origin_airport: "TSA",
        destination_airport: "HND",
      };
    case "KHH-BKK":
      // Codes not verified on Skyscanner/Trip.com: general search pages for those two.
      return {
        kind: "ok",
        cheapest_twd: 6480,
        checked_at: CHECKED_AT,
        currency: "TWD",
        depart_date: d(3),
        return_date: d(9),
        origin_airport: "KHH",
        destination_airport: "BKK",
      };
    case "TPE-LON":
      return { kind: "no_data", checked_at: CHECKED_AT };
    case "TPE-SYD":
      return { kind: "error", checked_at: CHECKED_AT };
    default:
      return null;
  }
}

function row(
  route: string,
  departure_month: string | null,
  target: number,
  extra: Partial<RouteSubscription> = {},
): RouteSubscription {
  const [origin = "", destination = ""] = route.split("-");
  return {
    id: subscriptionId(origin, destination, departure_month),
    route,
    origin,
    destination,
    departure_month,
    target_price: target,
    currency: "TWD",
    alerts_paused: false,
    plan_name: null,
    created_at: "2026-11-01T00:00:00.000Z",
    updated_at: "2026-11-01T00:00:00.000Z",
    ...extra,
  };
}

export function seedRows(scenario: MockScenario, now: Date): RouteSubscription[] {
  const month = currentMonth(now);
  const plus = (n: number) => addMonths(month, n);
  if (scenario === "empty") return [];
  if (scenario === "multi_month") return multiMonthRows(now);
  if (scenario === "full") {
    const dests = ["TYO", "OSA", "SEL", "BKK", "HKG", "SIN", "OKA", "MNL", "SGN", "KUL"];
    return dests.map((d, i) => row(`TPE-${d}`, plus(1 + (i % 12)), 5000 + i * 500));
  }
  return [
    row("TPE-TYO", null, 9000, { plan_name: "tokyo" }), // legacy M1 rolling row
    row("KHH-BKK", plus(2), 7000),
    row("KHH-OKA", plus(2), 4500, { alerts_paused: true, updated_at: "2026-11-03T00:00:00.000Z" }),
    row("RMQ-SEL", plus(-1), 6000, { created_at: "2026-09-20T00:00:00.000Z" }), // expired
    row("TPE-LON", plus(3), 28000),
    row("TPE-SYD", plus(1), 18000),
  ];
}

/** Demo with several months of one route beside the legacy rolling row (R-MM-1). */
export function multiMonthRows(now: Date): RouteSubscription[] {
  const plus = (n: number) => addMonths(currentMonth(now), n);
  return [
    row("TPE-TYO", null, 9000, { plan_name: "tokyo" }),
    row("TPE-TYO", plus(1), 8000, { created_at: "2026-11-02T00:00:00.000Z" }), // = rolling's month
    row("KHH-TYO", plus(2), 9000, { created_at: "2026-11-03T00:00:00.000Z" }),
    row("KHH-TYO", plus(3), 9500, { created_at: "2026-11-03T00:00:00.000Z" }),
    row("KHH-TYO", plus(4), 10000, {
      created_at: "2026-11-03T00:00:00.000Z",
      alerts_paused: true,
    }),
  ];
}

export class MockSubscriptionsClient implements SubscriptionsClient {
  readonly kind = "mock" as const;
  private rows: RouteSubscription[];
  private readonly fares = new Map<string, FareCheck | null>();
  private readonly now: () => Date;
  private readonly latencyMs: number;
  private scenario: MockScenario;
  private clock = 0;

  constructor(opts: MockOptions = {}) {
    // Defence in depth: production builds never construct the mock, even if some code path
    // tried to. Nothing here is persisted (no storage, no network): state lives in this object.
    if (!mockDataAllowed())
      throw new Error("Mock subscription data is not available in this build.");
    this.now = opts.now ?? (() => new Date());
    this.latencyMs = opts.latencyMs ?? 0;
    this.scenario = opts.scenario ?? "demo";
    this.rows = seedRows(this.scenario, this.now());
    this.seedFares();
  }

  /** Development control: switch to another scenario (resets all mock data). */
  setScenario(scenario: MockScenario): void {
    this.scenario = scenario;
    this.rows = seedRows(scenario, this.now());
    this.fares.clear();
    this.seedFares();
  }

  get currentScenario(): MockScenario {
    return this.scenario;
  }

  private seedFares() {
    for (const r of this.rows) {
      const month = effectiveMonth(r.departure_month, this.now());
      this.fares.set(`${r.route}#${month}`, demoFare(r.route, month));
    }
  }

  private async delay() {
    if (this.latencyMs > 0) await new Promise((res) => setTimeout(res, this.latencyMs));
  }

  /** Row versions, like the API's version_token: every row object the reducer produces (a new
   * object for every create, edit, move, pause or resume) gets its own random version. */
  private readonly versions = new WeakMap<RouteSubscription, string>();

  private versionOf(r: RouteSubscription): string {
    let v = this.versions.get(r);
    if (v === undefined) {
      v = randomVersion();
      this.versions.set(r, v);
    }
    return v;
  }

  private checkVersion(value: unknown): void {
    if (value === undefined || value === null) fail("version_required");
    if (typeof value !== "string" || !VERSION_RE.test(value)) fail("invalid_version");
  }

  /** Strictly increasing millisecond timestamps (informational updated_at). */
  private stamp(): string {
    const t = Math.max(this.now().getTime(), this.clock + 1);
    this.clock = t;
    return new Date(t).toISOString();
  }

  private dto(r: RouteSubscription): SubscriptionDto {
    const now = this.now();
    const month = effectiveMonth(r.departure_month, now);
    const name = (code: string) => {
      const a = airport(code);
      return a ? { zh: a.name_zh, en: a.name_en } : null;
    };
    return {
      ...r,
      version: this.versionOf(r),
      origin_name: name(r.origin),
      destination_name: name(r.destination),
      month_mode: r.departure_month === null ? "rolling" : "fixed",
      effective_month: month,
      status: statusOf(r, now),
      fare_check: parseFareCheck(this.fares.get(`${r.route}#${month}`) ?? null),
    };
  }

  async list(): Promise<ListResponse> {
    await this.delay();
    if (this.scenario === "list_error") fail("internal_error");
    if (this.scenario === "unauthorized") fail("unauthorized");
    const now = this.now();
    // Same order as the API: route pair, then rolling before fixed months, then month.
    const subs = [...this.rows].sort(
      (a, b) =>
        a.route.localeCompare(b.route) ||
        (a.departure_month ?? "").localeCompare(b.departure_month ?? "") ||
        a.created_at.localeCompare(b.created_at),
    );
    return {
      subscriptions: subs.map((r) => this.dto(r)),
      limits: { max: POLICY.maxSubscriptions, used: this.rows.length },
      months: { current: currentMonth(now), selectable: selectableMonths(now) },
    };
  }

  private check(
    input: { origin: string; destination: string; month: string; target: unknown },
    editing?: RouteSubscription,
  ): Validated {
    const targetText =
      typeof input.target === "number" && Number.isInteger(input.target)
        ? String(input.target)
        : "x";
    const result = validate(
      this.rows,
      { origin: input.origin, destination: input.destination, month: input.month, targetText },
      editing ? { editing, now: this.now() } : { now: this.now() },
    );
    // §4.2: the first failure in the documented order wins.
    if (!result.ok) {
      const code = result.errors[0] as ErrorCode;
      const month = /^\d{4}-\d{2}$/.test(input.month) ? input.month : null;
      return fail(
        code,
        code === "duplicate_subscription"
          ? { route: `${input.origin}-${input.destination}`, departure_month: month }
          : {},
      );
    }
    return result.value;
  }

  async create(req: CreateRequest): Promise<SubscriptionDto> {
    await this.delay();
    const value = this.check({
      origin: req.origin,
      destination: req.destination,
      month: req.departure_month ?? "",
      target: req.target_price,
    });
    const state = reducer(
      { subs: this.rows, lastError: null },
      { type: "create", value, now: this.stamp() },
    );
    if (state.lastError) fail(state.lastError.code);
    this.rows = state.subs;
    const id = subscriptionId(value.origin, value.destination, value.departure_month);
    return this.dto(this.rows.find((r) => r.id === id) as RouteSubscription);
  }

  /** `id` is the public subscription ID ("KHH-TYO" or "KHH-TYO_2027-01"). */
  async update(id: string, patch: PatchRequest): Promise<SubscriptionDto> {
    await this.delay();
    if (!parseSubscriptionId(id)) fail("invalid_route");
    const keys = Object.keys(patch).filter((k) => k !== "if_version");
    if (keys.length === 0) fail("empty_patch");
    if ((patch.origin === undefined) !== (patch.destination === undefined)) {
      fail("route_change_incomplete");
    }
    this.checkVersion(patch.if_version);
    const old = this.rows.find((r) => r.id === id);
    if (!old) return fail("not_found");
    // Never replaced by a freshly read version: a stale client gets `conflict` (§4.5).
    if (patch.if_version !== this.versionOf(old)) fail("conflict");
    let current = old;
    const touchesRoute = patch.origin !== undefined;
    if (touchesRoute || patch.target_price !== undefined || patch.departure_month !== undefined) {
      const month =
        patch.departure_month === undefined
          ? (old.departure_month ?? ROLLING)
          : (patch.departure_month ?? ROLLING);
      const value = this.check(
        {
          origin: patch.origin ?? old.origin,
          destination: patch.destination ?? old.destination,
          month,
          target: patch.target_price ?? old.target_price,
        },
        old,
      );
      const state = reducer(
        { subs: this.rows, lastError: null },
        { type: "edit", id, value, now: this.stamp() },
      );
      if (state.lastError) fail(state.lastError.code);
      this.rows = state.subs;
      const newId = subscriptionId(value.origin, value.destination, value.departure_month);
      current = this.rows.find((r) => r.id === newId) as RouteSubscription;
    }
    if (patch.alerts_paused !== undefined) {
      if (typeof patch.alerts_paused !== "boolean") fail("invalid_alerts_paused");
      const state = reducer(
        { subs: this.rows, lastError: null },
        { type: patch.alerts_paused ? "pause" : "resume", id: current.id, now: this.stamp() },
      );
      this.rows = state.subs;
      current = this.rows.find((r) => r.id === current.id) as RouteSubscription;
    }
    return this.dto(current);
  }

  async remove(id: string, ifVersion: string): Promise<void> {
    await this.delay();
    if (!parseSubscriptionId(id)) fail("invalid_route");
    this.checkVersion(ifVersion);
    const old = this.rows.find((r) => r.id === id);
    if (!old) return fail("not_found");
    // Only that exact version: a request for subscription A never deletes a B that reused A's ID.
    if (ifVersion !== this.versionOf(old)) fail("conflict");
    this.rows = this.rows.filter((r) => r.id !== id);
  }

  /** Test/development hook: simulate another tab changing a row (next write gets `conflict`). */
  touch(id: string): void {
    this.rows = this.rows.map((r) => (r.id === id ? { ...r, updated_at: this.stamp() } : r));
  }
}
