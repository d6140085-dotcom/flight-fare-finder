// M1.4 subscriptions API: wire types and the client interface the page talks to.
// Contract: docs/api/m1.4-subscriptions-api.md (§3 resources, §4 endpoints, §9 error codes).
// Phase 3A uses only MockSubscriptionsClient (mock-client.ts). Phase 3B adds HttpSubscriptionsClient
// (http-client.ts), which implements the same interface over HTTP; the page does not change.
import type { FareCheck } from "./fare-status";
import { errorMessage, type ErrorCode, type RouteSubscription, type Status } from "./subscriptions";

/** §3.1 Subscription as returned by the API (a superset of the prototype's RouteSubscription).
 * `id` is the public subscription ID used in PATCH/DELETE paths (R-MM-1); `route` is the pair. */
export interface SubscriptionDto extends RouteSubscription {
  origin_name: { zh: string; en: string } | null;
  destination_name: { zh: string; en: string } | null;
  month_mode: "fixed" | "rolling";
  effective_month: string;
  status: Status;
  /** §3.4: latest fare check for (origin, destination, effective_month); null = not checked yet. */
  fare_check: FareCheck | null;
  /** §4.5: opaque row version. Send it back as `if_version` on PATCH/DELETE; every successful
   * change returns a new one. Never derived from `updated_at` (which is informational only). */
  version: string;
}

/** §4.1 GET /subscriptions. */
export interface ListResponse {
  subscriptions: SubscriptionDto[];
  limits: { max: number; used: number };
  months: { current: string; selectable: string[] };
}

/** §4.2 POST /subscriptions. */
export interface CreateRequest {
  origin: string;
  destination: string;
  departure_month: string;
  target_price: number;
}

/** §4.3 PATCH /subscriptions/{id}. */
export interface PatchRequest {
  target_price?: number;
  departure_month?: string | null;
  alerts_paused?: boolean;
  origin?: string;
  destination?: string;
  /** §4.5: required — the `version` of the row this edit is based on. */
  if_version: string;
}

/** §9 error codes the API sends and the page handles. */
export const SERVER_ERROR_CODES = [
  "origin_required",
  "destination_required",
  "unsupported_origin",
  "unsupported_destination",
  "same_origin_destination",
  "invalid_target_price",
  "invalid_departure_month",
  "duplicate_subscription",
  "limit_reached",
  "not_found",
  "invalid_json",
  "invalid_route",
  "route_change_incomplete",
  "empty_patch",
  "unknown_field",
  "invalid_alerts_paused",
  "unauthorized",
  "conflict",
  "version_required",
  "invalid_version",
  "too_many_requests",
  "internal_error",
] as const;

/**
 * §9 error codes, plus codes only the HTTP client produces:
 * - `network_error`: no HTTP response (offline, DNS, CORS, timeout). A write may or may not
 *   have been applied.
 * - `service_unavailable`: 502/503/504 from the gateway or service. A write may or may not
 *   have been applied.
 * - `bad_request`: a 4xx without a known code (e.g. an unknown route or plan).
 * - `invalid_response`: a response that does not match the contract. After a successful write
 *   status the change may have been saved.
 */
export type ApiErrorCode =
  | ErrorCode
  | (typeof SERVER_ERROR_CODES)[number]
  | "network_error"
  | "service_unavailable"
  | "bad_request"
  | "invalid_response";

export class ApiError extends Error {
  constructor(
    readonly code: ApiErrorCode,
    readonly status: number,
    readonly details: Record<string, unknown> = {},
  ) {
    super(code);
    this.name = "ApiError";
  }
}

export interface SubscriptionsClient {
  /** "mock" in Phase 3A; "http" once Phase 3B wires the deployed API. */
  readonly kind: "mock" | "http";
  list(): Promise<ListResponse>;
  create(req: CreateRequest): Promise<SubscriptionDto>;
  /** `id`: the public subscription ID ("KHH-TYO" or "KHH-TYO_2027-01"), never the route pair. */
  update(id: string, patch: PatchRequest): Promise<SubscriptionDto>;
  /** `ifVersion`: required — the `version` of the row being deleted (§4.5). */
  remove(id: string, ifVersion: string): Promise<void>;
}

const SHARED: ReadonlySet<string> = new Set<ErrorCode>([
  "origin_required",
  "destination_required",
  "unsupported_origin",
  "unsupported_destination",
  "same_origin_destination",
  "invalid_target_price",
  "invalid_departure_month",
  "duplicate_subscription",
  "limit_reached",
  "not_found",
]);

/** Traditional Chinese text for an API error. Never echoes server text or tokens. */
export function apiErrorMessage(
  err: unknown,
  ctx: { origin?: string; destination?: string; departure_month?: string | null; now?: Date } = {},
): string {
  const code: ApiErrorCode = err instanceof ApiError ? err.code : "network_error";
  if (SHARED.has(code)) {
    const month = err instanceof ApiError ? err.details["departure_month"] : undefined;
    return errorMessage(code as ErrorCode, {
      ...ctx,
      ...(typeof month === "string" || month === null ? { departure_month: month } : {}),
    });
  }
  switch (code) {
    case "unauthorized":
      return "登入已過期，請重新登入。";
    case "conflict":
      return "這筆訂閱剛被其他裝置或分頁修改，已重新載入最新資料，請再試一次。";
    case "too_many_requests":
      return "操作太頻繁，請稍候幾秒再試。";
    case "network_error":
      return "無法連線到訂閱服務，請檢查網路後再試一次。";
    case "internal_error":
      return "訂閱服務暫時發生錯誤，沒有任何變更被儲存，請稍後再試。";
    case "service_unavailable":
      return "訂閱服務暫時無法使用，請稍後重新整理頁面確認最新狀態後再試。";
    case "invalid_response":
      return "訂閱服務回應異常，請重新整理頁面確認最新狀態。";
    case "version_required":
      return "這筆訂閱缺少版本資訊，請重新整理頁面後再試一次。";
    default:
      return "請求內容有誤，請重新整理頁面後再試一次。";
  }
}

// ---- defensive parsing of fare_check (§3.4) ------------------------------------------------

const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Validates an API fare_check. Unknown or malformed data is never shown as a fare:
 * - null/undefined -> null (not checked yet);
 * - `ok` without a positive whole TWD price (0, negative, fractional, missing, NaN) -> `error`,
 *   so "no data" or a broken record can never render as NT$0;
 * - an unknown kind or a missing checked_at -> null.
 */
export function parseFareCheck(raw: unknown): FareCheck | null {
  if (raw === null || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const checkedAt = r["checked_at"];
  if (typeof checkedAt !== "string" || !ISO_INSTANT.test(checkedAt)) return null;
  switch (r["kind"]) {
    case "no_data":
      return { kind: "no_data", checked_at: checkedAt };
    case "error":
      return { kind: "error", checked_at: checkedAt };
    case "ok": {
      const price = r["cheapest_twd"];
      if (typeof price !== "number" || !Number.isInteger(price) || price <= 0) {
        return { kind: "error", checked_at: checkedAt };
      }
      if (r["currency"] !== undefined && r["currency"] !== "TWD") {
        return { kind: "error", checked_at: checkedAt };
      }
      const date = (k: string) =>
        typeof r[k] === "string" && ISO_DATE.test(r[k] as string) ? (r[k] as string) : undefined;
      const airportCode = (k: string) =>
        typeof r[k] === "string" && /^[A-Z]{3}$/.test(r[k] as string) ? (r[k] as string) : null;
      const out: FareCheck = {
        kind: "ok",
        cheapest_twd: price,
        checked_at: checkedAt,
        currency: "TWD",
        origin_airport: airportCode("origin_airport"),
        destination_airport: airportCode("destination_airport"),
      };
      const dep = date("depart_date");
      const ret = date("return_date");
      if (dep) out.depart_date = dep;
      if (ret) out.return_date = ret;
      return out;
    }
    default:
      return null;
  }
}
