// M1.4 Phase 3B-A: SubscriptionsClient over HTTP (docs/api/m1.4-subscriptions-api.md §4, §9).
//
// Not selected anywhere yet: the page still uses MockSubscriptionsClient (client-factory.ts).
// This module never reads environment variables or a default URL. The caller must pass
// `baseUrl` explicitly, so importing it cannot reach any API by accident.
//
// Rules:
// - Identity comes only from the Supabase bearer token. No email or user ID is ever sent.
// - `if_version` is sent exactly as given: PATCH body, DELETE query (§4.5).
// - No retries. A failed or stale write is reported (`ApiError`), never replayed with a freshly
//   read version; the page reloads on `conflict` / `not_found`.
// - Responses are validated against the contract; anything else is `invalid_response`.
// - Nothing is logged.
import { getAccessToken } from "@/lib/flight-api";
import {
  ApiError,
  SERVER_ERROR_CODES,
  parseFareCheck,
  type ApiErrorCode,
  type CreateRequest,
  type ListResponse,
  type PatchRequest,
  type SubscriptionDto,
  type SubscriptionsClient,
} from "./api";
import { MONTH_RE } from "./months";
import { parseSubscriptionId } from "./subscription-id";

export interface HttpClientOptions {
  /** Absolute http(s) base URL of the subscriptions API, without query or fragment. */
  baseUrl: string;
  /** Supabase access token of the signed-in user, or null. Default: the shared M1 helper. */
  getToken?: () => Promise<string | null>;
  /** Injectable for tests. Default: the global fetch. */
  fetch?: typeof fetch;
  /** Abort a request after this many milliseconds (`network_error`). Default 20 000. */
  timeoutMs?: number;
}

const KNOWN_CODES: ReadonlySet<string> = new Set(SERVER_ERROR_CODES);
const VERSION_RE = /^(?:[A-Za-z0-9_-]{22}|legacy-[0-9a-f]{32})$/; // = validation.py VERSION_RE
const AIRPORT_RE = /^[A-Z]{3}$/;
const PATCH_FIELDS = [
  "target_price",
  "departure_month",
  "alerts_paused",
  "origin",
  "destination",
  "if_version",
] as const;

export class HttpSubscriptionsClient implements SubscriptionsClient {
  readonly kind = "http" as const;
  private readonly base: string;
  private readonly getToken: () => Promise<string | null>;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;

  constructor(opts: HttpClientOptions) {
    this.base = normaliseBaseUrl(opts.baseUrl);
    this.getToken = opts.getToken ?? getAccessToken;
    this.fetchImpl = opts.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.timeoutMs = opts.timeoutMs ?? 20_000;
  }

  async list(): Promise<ListResponse> {
    const res = await this.send("GET", "/subscriptions");
    return parseList(await this.success(res, 200));
  }

  async create(req: CreateRequest): Promise<SubscriptionDto> {
    // Only the four contract fields: nothing else from the caller's object reaches the API.
    const body = {
      origin: req.origin,
      destination: req.destination,
      departure_month: req.departure_month,
      target_price: req.target_price,
    };
    const res = await this.send("POST", "/subscriptions", body);
    return parseSubscriptionOrThrow(await this.success(res, 201), res.status);
  }

  async update(id: string, patch: PatchRequest): Promise<SubscriptionDto> {
    const body: Record<string, unknown> = {};
    for (const field of PATCH_FIELDS) {
      if (patch[field] !== undefined) body[field] = patch[field];
    }
    const res = await this.send("PATCH", `/subscriptions/${encodeURIComponent(id)}`, body);
    return parseSubscriptionOrThrow(await this.success(res, 200), res.status);
  }

  async remove(id: string, ifVersion: string): Promise<void> {
    const query =
      ifVersion === undefined || ifVersion === null
        ? ""
        : `?if_version=${encodeURIComponent(ifVersion)}`;
    const res = await this.send("DELETE", `/subscriptions/${encodeURIComponent(id)}${query}`);
    if (res.status !== 204) {
      if (res.ok) throw new ApiError("invalid_response", res.status);
      throw await errorFrom(res);
    }
  }

  // ---- transport ---------------------------------------------------------------------------

  private async send(method: string, path: string, body?: unknown): Promise<Response> {
    const token = await this.getToken();
    if (!token) throw new ApiError("unauthorized", 401);
    const headers: Record<string, string> = {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
    };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const init: RequestInit = {
        method,
        headers,
        credentials: "omit", // no cookies: the bearer token is the only credential
        cache: "no-store",
        redirect: "error", // never follow a redirect with the token
        signal: controller.signal,
      };
      if (body !== undefined) init.body = JSON.stringify(body);
      return await this.fetchImpl(`${this.base}${path}`, init);
    } catch {
      // Offline, DNS, CORS, redirect or timeout: there is no HTTP response to read.
      throw new ApiError("network_error", 0);
    } finally {
      clearTimeout(timer);
    }
  }

  /** The JSON body of an expected success status; otherwise the structured error. */
  private async success(res: Response, expected: number): Promise<unknown> {
    if (res.status !== expected) {
      if (res.ok) throw new ApiError("invalid_response", res.status);
      throw await errorFrom(res);
    }
    const body = await readJson(res);
    if (body === undefined) throw new ApiError("invalid_response", res.status);
    return body;
  }
}

// ---- errors ----------------------------------------------------------------------------------

/** Maps a non-2xx response to an ApiError (status, stable code, safe details only). */
export async function errorFrom(res: Response): Promise<ApiError> {
  const status = res.status;
  // Gateway-level answers carry no contract body (`{"message": ...}` or HTML).
  if (status === 429) return new ApiError("too_many_requests", status);
  if (status === 502 || status === 503 || status === 504) {
    return new ApiError("service_unavailable", status);
  }
  const body = await readJson(res);
  const raw = isRecord(body) ? body["error"] : undefined;
  if (status >= 400 && status < 500 && typeof raw === "string" && KNOWN_CODES.has(raw)) {
    return new ApiError(raw as ApiErrorCode, status, safeDetails(body));
  }
  return new ApiError(fallbackCode(status), status);
}

function fallbackCode(status: number): ApiErrorCode {
  if (status === 401 || status === 403) return "unauthorized";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 428) return "version_required";
  if (status >= 500) return "internal_error";
  return "bad_request";
}

/** Keeps only the documented, non-sensitive detail fields (§9), with their expected types. */
function safeDetails(body: unknown): Record<string, unknown> {
  const raw = isRecord(body) ? body["details"] : undefined;
  if (!isRecord(raw)) return {};
  const out: Record<string, unknown> = {};
  const route = raw["route"];
  if (typeof route === "string" && /^[A-Z]{3}-[A-Z]{3}$/.test(route)) out["route"] = route;
  const month = raw["departure_month"];
  if (month === null || (typeof month === "string" && MONTH_RE.test(month))) {
    out["departure_month"] = month;
  }
  for (const k of ["max", "used"] as const) {
    const n = raw[k];
    if (typeof n === "number" && Number.isInteger(n) && n >= 0) out[k] = n;
  }
  const fields = raw["fields"];
  if (Array.isArray(fields)) {
    out["fields"] = fields
      .filter((f): f is string => typeof f === "string" && /^[a-z_]{1,40}$/.test(f))
      .slice(0, 20);
  }
  return out;
}

// ---- response parsing -------------------------------------------------------------------------

async function readJson(res: Response): Promise<unknown> {
  try {
    const text = await res.text();
    return text.trim() === "" ? undefined : (JSON.parse(text) as unknown);
  } catch {
    return undefined;
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

const isMonth = (v: unknown): v is string => typeof v === "string" && MONTH_RE.test(v);
const isCount = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;

function parseName(v: unknown): { zh: string; en: string } | null | undefined {
  if (v === null) return null;
  if (isRecord(v) && typeof v["zh"] === "string" && typeof v["en"] === "string") {
    return { zh: v["zh"], en: v["en"] };
  }
  return undefined;
}

function nullableString(v: unknown): string | null | undefined {
  return v === null || typeof v === "string" ? v : undefined;
}

/** §3.1 Subscription; null when any field breaks the contract. Unknown extra fields are dropped. */
export function parseSubscription(raw: unknown): SubscriptionDto | null {
  if (!isRecord(raw)) return null;
  const id = raw["id"];
  const parsedId = parseSubscriptionId(id);
  const origin = raw["origin"];
  const destination = raw["destination"];
  const month = raw["departure_month"];
  const monthMode = raw["month_mode"];
  const status = raw["status"];
  const target = raw["target_price"];
  const version = raw["version"];
  const originName = parseName(raw["origin_name"]);
  const destinationName = parseName(raw["destination_name"]);
  const planName = nullableString(raw["plan_name"]);
  const createdAt = nullableString(raw["created_at"]);
  const updatedAt = nullableString(raw["updated_at"]);
  if (
    typeof id !== "string" ||
    !parsedId ||
    typeof origin !== "string" ||
    typeof destination !== "string" ||
    !AIRPORT_RE.test(origin) ||
    !AIRPORT_RE.test(destination) ||
    parsedId.origin !== origin ||
    parsedId.destination !== destination ||
    raw["route"] !== `${origin}-${destination}` ||
    !(month === null || isMonth(month)) ||
    parsedId.month !== month ||
    monthMode !== (month === null ? "rolling" : "fixed") ||
    !isMonth(raw["effective_month"]) ||
    (status !== "active" && status !== "paused" && status !== "expired") ||
    typeof target !== "number" ||
    !Number.isInteger(target) ||
    target <= 0 ||
    raw["currency"] !== "TWD" ||
    typeof raw["alerts_paused"] !== "boolean" ||
    typeof version !== "string" ||
    !VERSION_RE.test(version) ||
    originName === undefined ||
    destinationName === undefined ||
    planName === undefined ||
    createdAt === undefined ||
    updatedAt === undefined
  ) {
    return null;
  }
  return {
    id,
    route: `${origin}-${destination}`,
    origin,
    destination,
    origin_name: originName,
    destination_name: destinationName,
    departure_month: month,
    month_mode: month === null ? "rolling" : "fixed",
    effective_month: raw["effective_month"] as string,
    status,
    target_price: target,
    currency: "TWD",
    alerts_paused: raw["alerts_paused"] as boolean,
    plan_name: planName,
    created_at: createdAt,
    updated_at: updatedAt,
    version,
    fare_check: parseFareCheck(raw["fare_check"]),
  };
}

function parseSubscriptionOrThrow(raw: unknown, status: number): SubscriptionDto {
  const sub = parseSubscription(raw);
  if (!sub) throw new ApiError("invalid_response", status);
  return sub;
}

/** §4.1 list response; throws `invalid_response` when it breaks the contract. */
export function parseList(raw: unknown): ListResponse {
  const bad = () => new ApiError("invalid_response", 200);
  if (!isRecord(raw) || !Array.isArray(raw["subscriptions"])) throw bad();
  const limits = raw["limits"];
  const months = raw["months"];
  if (!isRecord(limits) || !isCount(limits["max"]) || !isCount(limits["used"])) throw bad();
  if (limits["max"] < 1 || limits["used"] !== raw["subscriptions"].length) throw bad();
  if (!isRecord(months) || !isMonth(months["current"]) || !Array.isArray(months["selectable"])) {
    throw bad();
  }
  if (!months["selectable"].every(isMonth)) throw bad();
  const subscriptions: SubscriptionDto[] = [];
  const ids = new Set<string>();
  for (const item of raw["subscriptions"]) {
    const sub = parseSubscription(item);
    if (!sub || ids.has(sub.id)) throw bad();
    ids.add(sub.id);
    subscriptions.push(sub);
  }
  return {
    subscriptions,
    limits: { max: limits["max"], used: limits["used"] },
    months: { current: months["current"], selectable: [...months["selectable"]] },
  };
}

function normaliseBaseUrl(input: string): string {
  let url: URL;
  try {
    url = new URL(input);
  } catch {
    throw new Error("HttpSubscriptionsClient: baseUrl must be an absolute URL.");
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error("HttpSubscriptionsClient: baseUrl must use http or https.");
  }
  if (url.search || url.hash || url.username || url.password) {
    throw new Error(
      "HttpSubscriptionsClient: baseUrl must not contain a query, fragment or credentials.",
    );
  }
  return url.href.replace(/\/+$/, "");
}
