// Unit tests for HttpSubscriptionsClient (Phase 3B-A): transport, error mapping, response
// validation, safety and isolation. The shared request/response examples are checked in
// http-client.contract.test.ts. No test reaches a network.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import vectors from "../../../aws/tests/vectors/subscriptions-http.json";
import { ApiError, apiErrorMessage, type PatchRequest } from "./api";
import { HttpSubscriptionsClient, errorFrom, parseList, parseSubscription } from "./http-client";

const BASE = "https://subscriptions-api.example.test";
const TOKEN = "test-access-token";
const listBody = vectors.cases.find((c) => c.name === "list_mixed")!.response.body as Record<
  string,
  unknown
>;
const sub = () =>
  structuredClone(
    (listBody["subscriptions"] as Record<string, unknown>[]).find(
      (s) => s["id"] === "TPE-OSA_2027-01",
    )!,
  );

type Reply = Response | Error | ((init: RequestInit) => Promise<Response>);

function setup(reply: Reply, opts: { token?: string | null; timeoutMs?: number } = {}) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    if (reply instanceof Error) throw reply;
    if (typeof reply === "function") return reply(init ?? {});
    return reply.clone();
  });
  const client = new HttpSubscriptionsClient({
    baseUrl: BASE,
    getToken: async () => (opts.token === undefined ? TOKEN : opts.token),
    fetch,
    ...(opts.timeoutMs !== undefined ? { timeoutMs: opts.timeoutMs } : {}),
  });
  return { client, fetch, calls };
}

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
const text = (status: number, body: string, type = "text/html") =>
  new Response(body, { status, headers: { "Content-Type": type } });

async function rejection(p: Promise<unknown>): Promise<ApiError> {
  const err = await p.then(
    () => {
      throw new Error("expected the call to fail");
    },
    (e: unknown) => e,
  );
  expect(err).toBeInstanceOf(ApiError);
  return err as ApiError;
}

const patch: PatchRequest = { target_price: 7500, if_version: "fixedJanVersion0000000" };

describe("requests", () => {
  it("lists with the bearer token and no body, cookies, cache or redirects", async () => {
    const { client, calls } = setup(json(200, listBody));
    await client.list();
    const { url, init } = calls[0]!;
    expect(url).toBe(`${BASE}/subscriptions`);
    expect(init.method).toBe("GET");
    expect(init.headers).toEqual({ Accept: "application/json", Authorization: `Bearer ${TOKEN}` });
    expect(init.body).toBeUndefined();
    expect(init.credentials).toBe("omit");
    expect(init.cache).toBe("no-store");
    expect(init.redirect).toBe("error");
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it("without a session it fails as unauthorized and sends nothing", async () => {
    for (const token of [null, ""]) {
      const { client, fetch } = setup(json(200, listBody), { token });
      const err = await rejection(client.list());
      expect([err.code, err.status]).toEqual(["unauthorized", 401]);
      expect(fetch).not.toHaveBeenCalled();
    }
  });

  it("create sends only the four contract fields — never an email or user id", async () => {
    const { client, calls } = setup(json(201, sub()));
    const req = {
      origin: "TPE",
      destination: "OSA",
      departure_month: "2027-01",
      target_price: 8000,
      email: "other@example.com",
      user_id: "u-1",
    };
    await client.create(req);
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual({
      origin: "TPE",
      destination: "OSA",
      departure_month: "2027-01",
      target_price: 8000,
    });
  });

  it("update sends only patch fields that are set, and if_version exactly as given", async () => {
    const { client, calls } = setup(json(200, sub()));
    const odd = "Ab_-0123456789abcdefgh";
    await client.update("TPE-OSA_2027-01", {
      if_version: odd,
      departure_month: null,
      alerts_paused: false,
      email: "other@example.com",
    } as PatchRequest);
    expect(calls[0]!.url).toBe(`${BASE}/subscriptions/TPE-OSA_2027-01`);
    expect(calls[0]!.init.method).toBe("PATCH");
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual({
      if_version: odd,
      departure_month: null,
      alerts_paused: false,
    });
  });

  it("percent-encodes the id path segment and the if_version query value", async () => {
    const { client, calls } = setup(new Response(null, { status: 204 }));
    await client.remove("a/b?c#d e", "v+1&x=y/%");
    expect(calls[0]!.url).toBe(
      `${BASE}/subscriptions/a%2Fb%3Fc%23d%20e?if_version=v%2B1%26x%3Dy%2F%25`,
    );
    expect(calls[0]!.init.method).toBe("DELETE");
    expect(calls[0]!.init.body).toBeUndefined();
  });

  it("delete preserves a legacy version exactly and resolves on 204 with an empty body", async () => {
    const { client, calls } = setup(new Response(null, { status: 204 }));
    const legacy = "legacy-4d1c2f0216c0346b4dfda29c5c271256";
    await expect(client.remove("TPE-TYO", legacy)).resolves.toBeUndefined();
    expect(new URL(calls[0]!.url).searchParams.get("if_version")).toBe(legacy);
  });

  it("normalises the base URL and rejects unsafe ones", () => {
    const c = new HttpSubscriptionsClient({ baseUrl: `${BASE}/prod///`, fetch: vi.fn() });
    expect(c.kind).toBe("http");
    for (const bad of [
      "",
      "/relative",
      "ftp://api.example.test",
      `${BASE}?stage=prod`,
      `${BASE}#x`,
      "https://user:pw@api.example.test",
    ]) {
      expect(() => new HttpSubscriptionsClient({ baseUrl: bad, fetch: vi.fn() })).toThrow();
    }
  });

  it("joins a base path without doubling slashes", async () => {
    const fetch = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
      json(200, listBody),
    );
    await new HttpSubscriptionsClient({
      baseUrl: `${BASE}/prod/`,
      getToken: async () => TOKEN,
      fetch,
    }).list();
    expect(String(fetch.mock.calls[0]![0])).toBe(`${BASE}/prod/subscriptions`);
  });
});

describe("error mapping", () => {
  const cases: [string, () => Response, string, number, Record<string, unknown>][] = [
    [
      "400 validation code",
      () => json(400, { error: "invalid_target_price", message: "x" }),
      "invalid_target_price",
      400,
      {},
    ],
    [
      "400 unknown_field keeps field names only",
      () => json(400, { error: "unknown_field", details: { fields: ["email", "Bad Field", 3] } }),
      "unknown_field",
      400,
      { fields: ["email"] },
    ],
    ["401 from the API", () => json(401, { error: "unauthorized" }), "unauthorized", 401, {}],
    ["401 from the gateway", () => json(401, { message: "Unauthorized" }), "unauthorized", 401, {}],
    ["403 from the gateway", () => json(403, { message: "Forbidden" }), "unauthorized", 403, {}],
    ["404 not_found", () => json(404, { error: "not_found" }), "not_found", 404, {}],
    ["404 gateway route", () => json(404, { message: "Not Found" }), "not_found", 404, {}],
    ["409 conflict", () => json(409, { error: "conflict" }), "conflict", 409, {}],
    [
      "409 duplicate keeps route and month",
      () =>
        json(409, {
          error: "duplicate_subscription",
          details: { route: "TPE-OSA", departure_month: "2027-01", email: "user@example.com" },
        }),
      "duplicate_subscription",
      409,
      { route: "TPE-OSA", departure_month: "2027-01" },
    ],
    [
      "409 limit keeps max and used",
      () => json(409, { error: "limit_reached", details: { max: 10, used: 10, token: "secret" } }),
      "limit_reached",
      409,
      { max: 10, used: 10 },
    ],
    ["409 without a body", () => text(409, ""), "conflict", 409, {}],
    [
      "428 version_required",
      () => json(428, { error: "version_required" }),
      "version_required",
      428,
      {},
    ],
    ["428 without a code", () => json(428, {}), "version_required", 428, {}],
    [
      "429 from the gateway (not a contract body)",
      () => json(429, { message: "Too Many Requests" }),
      "too_many_requests",
      429,
      {},
    ],
    ["429 with HTML", () => text(429, "<html>slow down</html>"), "too_many_requests", 429, {}],
    ["500 internal_error", () => json(500, { error: "internal_error" }), "internal_error", 500, {}],
    ["500 with HTML", () => text(500, "<html>oops</html>"), "internal_error", 500, {}],
    [
      "500 never trusts a 4xx code",
      () => json(500, { error: "limit_reached", details: { max: 10, used: 10 } }),
      "internal_error",
      500,
      {},
    ],
    ["502", () => json(502, { message: "Internal Server Error" }), "service_unavailable", 502, {}],
    ["503", () => text(503, "Service Unavailable", "text/plain"), "service_unavailable", 503, {}],
    [
      "504",
      () => json(504, { message: "Endpoint request timed out" }),
      "service_unavailable",
      504,
      {},
    ],
    [
      "unknown 4xx code (e.g. invalid_plan)",
      () => json(400, { error: "invalid_plan" }),
      "bad_request",
      400,
      {},
    ],
    ["413 from the gateway", () => text(413, "too large"), "bad_request", 413, {}],
    [
      "error that is not a string",
      () => json(400, { error: { code: "x" } }),
      "bad_request",
      400,
      {},
    ],
  ];

  it.each(cases)("%s", async (_name, response, code, status, details) => {
    const err = await errorFrom(response());
    expect([err.code, err.status]).toEqual([code, status]);
    expect(err.details).toEqual(details);
  });

  it.each(cases)(
    "%s — surfaces from every operation, once, without retry",
    async (_n, response, code) => {
      const ops = [
        (c: HttpSubscriptionsClient) => c.list(),
        (c: HttpSubscriptionsClient) =>
          c.create({
            origin: "TPE",
            destination: "OSA",
            departure_month: "2027-01",
            target_price: 1,
          }),
        (c: HttpSubscriptionsClient) => c.update("TPE-OSA_2027-01", patch),
        (c: HttpSubscriptionsClient) => c.remove("TPE-OSA_2027-01", patch.if_version),
      ];
      for (const op of ops) {
        const { client, fetch } = setup(response());
        expect((await rejection(op(client))).code).toBe(code);
        expect(fetch).toHaveBeenCalledTimes(1);
      }
    },
  );

  it("keeps a month of null in duplicate details and drops malformed values", async () => {
    const err = await errorFrom(
      json(409, {
        error: "duplicate_subscription",
        details: { route: "TPE-TYO", departure_month: null, max: -1, used: 1.5, fields: "x" },
      }),
    );
    expect(err.details).toEqual({ route: "TPE-TYO", departure_month: null });
  });

  it("network and CORS failures are network_error, without retry", async () => {
    for (const failure of [new TypeError("Failed to fetch"), new Error("redirect")]) {
      const { client, fetch } = setup(failure);
      const err = await rejection(client.update("TPE-OSA_2027-01", patch));
      expect([err.code, err.status]).toEqual(["network_error", 0]);
      expect(fetch).toHaveBeenCalledTimes(1);
    }
  });

  it("a request that takes too long is aborted as network_error", async () => {
    const { client } = setup(
      (init) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
      { timeoutMs: 5 },
    );
    expect((await rejection(client.list())).code).toBe("network_error");
  });

  it("a stale write is reported, never refreshed and replayed", async () => {
    const { client, fetch } = setup(json(409, { error: "conflict" }));
    expect((await rejection(client.update("TPE-OSA_2027-01", patch))).code).toBe("conflict");
    expect((await rejection(client.remove("TPE-OSA_2027-01", patch.if_version))).code).toBe(
      "conflict",
    );
    expect(fetch).toHaveBeenCalledTimes(2); // one request per call; no list() to fetch a new version
    expect(fetch.mock.calls.every(([, init]) => init?.method !== "GET")).toBe(true);
  });

  it("has a Traditional Chinese message for every client-side code, without server text", () => {
    for (const code of [
      "network_error",
      "service_unavailable",
      "invalid_response",
      "version_required",
      "too_many_requests",
      "bad_request",
    ] as const) {
      const msg = apiErrorMessage(new ApiError(code, 0));
      expect(msg).toMatch(/[一-鿿]/);
      expect(msg).not.toMatch(/Too Many|Internal|html/i);
    }
    expect(apiErrorMessage(new ApiError("service_unavailable", 503))).toContain("重新整理");
    expect(apiErrorMessage(new ApiError("invalid_response", 200))).toContain("重新整理");
  });
});

describe("response validation", () => {
  it("parses a documented subscription exactly and drops unknown fields", () => {
    const raw = { ...sub(), email: "user@example.com", extra: 1 };
    const parsed = parseSubscription(raw);
    expect(parsed).toEqual(sub());
    expect(parsed).not.toHaveProperty("email");
  });

  it("accepts null legacy timestamps, names and plan", () => {
    const raw = {
      ...sub(),
      created_at: null,
      updated_at: null,
      origin_name: null,
      plan_name: null,
    };
    expect(parseSubscription(raw)).toMatchObject({ created_at: null, updated_at: null });
  });

  const broken: [string, (s: Record<string, unknown>) => void][] = [
    ["missing id", (s) => delete s["id"]],
    ["id not a public id", (s) => (s["id"] = "TPE-OSA#2027-01")],
    ["id disagrees with month", (s) => (s["id"] = "TPE-OSA_2027-02")],
    ["id disagrees with origin", (s) => (s["origin"] = "KHH")],
    ["route disagrees", (s) => (s["route"] = "TPE-TYO")],
    ["month_mode disagrees", (s) => (s["month_mode"] = "rolling")],
    ["bad effective_month", (s) => (s["effective_month"] = "2027-13")],
    ["unknown status", (s) => (s["status"] = "deleted")],
    ["zero target", (s) => (s["target_price"] = 0)],
    ["fractional target", (s) => (s["target_price"] = 100.5)],
    ["string target", (s) => (s["target_price"] = "8000")],
    ["other currency", (s) => (s["currency"] = "USD")],
    ["alerts_paused not boolean", (s) => (s["alerts_paused"] = "false")],
    ["missing version", (s) => delete s["version"]],
    ["malformed version", (s) => (s["version"] = "short")],
    ["timestamp not string or null", (s) => (s["created_at"] = 0)],
    ["missing timestamp", (s) => delete s["updated_at"]],
    ["bad name", (s) => (s["origin_name"] = { zh: "台北" })],
    ["plan_name number", (s) => (s["plan_name"] = 1)],
  ];

  it.each(broken)("rejects a subscription with %s", (_name, mutate) => {
    const raw = sub();
    mutate(raw);
    expect(parseSubscription(raw)).toBeNull();
  });

  it("a malformed fare_check is never shown as a fare", () => {
    const raw = {
      ...sub(),
      fare_check: { kind: "ok", cheapest_twd: 0, checked_at: "2026-11-05T00:00:00Z" },
    };
    expect(parseSubscription(raw)?.fare_check).toEqual({
      kind: "error",
      checked_at: "2026-11-05T00:00:00Z",
    });
  });

  const badLists: [string, (l: Record<string, unknown>) => void][] = [
    ["subscriptions not an array", (l) => (l["subscriptions"] = {})],
    [
      "one broken subscription",
      (l) => ((l["subscriptions"] as Record<string, unknown>[])[0]!["status"] = "x"),
    ],
    [
      "duplicate ids",
      (l) => (l["subscriptions"] as unknown[]).push((l["subscriptions"] as unknown[])[0]),
    ],
    ["used != number of rows", (l) => ((l["limits"] as Record<string, unknown>)["used"] = 4)],
    ["max not a positive integer", (l) => ((l["limits"] as Record<string, unknown>)["max"] = 0)],
    ["missing months", (l) => delete l["months"]],
    [
      "bad selectable month",
      (l) => ((l["months"] as Record<string, unknown>)["selectable"] = ["2027-1"]),
    ],
  ];

  it.each(badLists)("rejects a list with %s", (_name, mutate) => {
    const raw = structuredClone(listBody);
    mutate(raw);
    expect(() => parseList(raw)).toThrow(ApiError);
  });

  it("success statuses with an unusable body are invalid_response", async () => {
    const make = (r: Response) => setup(r).client;
    for (const r of [text(200, "<html>ok</html>"), text(200, ""), json(200, [])]) {
      expect((await rejection(make(r).list())).code).toBe("invalid_response");
    }
    expect(
      (
        await rejection(
          make(json(201, { ...sub(), version: 1 })).create({
            origin: "TPE",
            destination: "OSA",
            departure_month: "2027-01",
            target_price: 8000,
          }),
        )
      ).code,
    ).toBe("invalid_response");
    expect((await rejection(make(text(200, "")).update("TPE-OSA_2027-01", patch))).code).toBe(
      "invalid_response",
    );
  });

  it("an unexpected success status is invalid_response (strict contract)", async () => {
    const created = await rejection(
      setup(json(200, sub())).client.create({
        origin: "TPE",
        destination: "OSA",
        departure_month: "2027-01",
        target_price: 8000,
      }),
    );
    expect([created.code, created.status]).toEqual(["invalid_response", 200]);
    const removed = await rejection(
      setup(json(200, {})).client.remove("TPE-TYO", patch.if_version),
    );
    expect([removed.code, removed.status]).toEqual(["invalid_response", 200]);
  });
});

describe("safety and isolation", () => {
  it("never logs (tokens, emails or bodies cannot leak to the console)", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((m) =>
      vi.spyOn(console, m).mockImplementation(() => undefined),
    );
    await setup(json(200, listBody)).client.list();
    await rejection(
      setup(json(409, { error: "conflict" })).client.update("TPE-OSA_2027-01", patch),
    );
    await rejection(setup(new TypeError("Failed to fetch")).client.list());
    for (const spy of spies) {
      expect(spy).not.toHaveBeenCalled();
      spy.mockRestore();
    }
  });

  const SRC = join(__dirname, "..", "..");
  const files = (dir: string): string[] =>
    readdirSync(dir).flatMap((f) => {
      const p = join(dir, f);
      return statSync(p).isDirectory() ? files(p) : [p];
    });

  it("is not imported by any application module (only by its own tests)", () => {
    const importers = files(SRC)
      .filter((f) => /\.(ts|tsx)$/.test(f) && !/\.test\.(ts|tsx)$/.test(f))
      .filter((f) => !f.endsWith(join("m14", "http-client.ts")))
      .filter((f) => /from\s+["'][^"']*http-client["']/.test(readFileSync(f, "utf8")))
      .map((f) => relative(SRC, f));
    expect(importers).toEqual([]);
  });

  it("reads no environment variable and contains no API URL", () => {
    const source = readFileSync(join(__dirname, "http-client.ts"), "utf8");
    expect(source).not.toMatch(/import\.meta\.env|process\.env|VITE_/);
    expect(source).not.toMatch(/https?:\/\/[a-z0-9.-]+\.[a-z]{2,}/i);
    expect(source).not.toMatch(/console\./);
  });

  it("the client factory still builds only the mock client", () => {
    const factory = readFileSync(join(__dirname, "client-factory.ts"), "utf8");
    expect(factory).toContain("MockSubscriptionsClient");
    expect(factory).not.toMatch(/HttpSubscriptionsClient|http-client/);
  });
});
