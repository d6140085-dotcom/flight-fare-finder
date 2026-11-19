// Shared HTTP contract examples (aws/tests/vectors/subscriptions-http.json), frontend side.
// The same examples are replayed through the real Lambda handler by
// aws/tests/test_m14_http_contract.py, so client and API cannot drift apart unnoticed.
// For every example HttpSubscriptionsClient must (1) send exactly the example's request and
// (2) return, or throw, exactly what the example's response means.
import vectors from "../../../aws/tests/vectors/subscriptions-http.json";
import { ApiError, type CreateRequest, type PatchRequest } from "./api";
import { HttpSubscriptionsClient } from "./http-client";

interface HttpPart {
  method: string;
  path: string;
  query: Record<string, string> | null;
  body: Record<string, unknown> | null;
}
interface Case {
  name: string;
  client: {
    op: "list" | "create" | "update" | "remove";
    id?: string;
    request?: CreateRequest;
    patch?: PatchRequest;
    if_version?: string | null;
  };
  http: HttpPart;
  response: { status: number; body: Record<string, unknown> | null };
}

const cases = vectors.cases as unknown as Case[];

function fakeFetch(response: Case["response"]) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    const body = response.body === null ? null : JSON.stringify(response.body);
    return new Response(body, {
      status: response.status,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  });
  return { fetch, calls };
}

function call(client: HttpSubscriptionsClient, c: Case): Promise<unknown> {
  switch (c.client.op) {
    case "list":
      return client.list();
    case "create":
      return client.create(c.client.request as CreateRequest);
    case "update":
      return client.update(c.client.id as string, c.client.patch as PatchRequest);
    case "remove":
      // A missing version is part of the contract (428), so the example may pass none.
      return client.remove(c.client.id as string, c.client.if_version as string);
  }
}

function expectedUrl(http: HttpPart): string {
  const query = http.query
    ? "?" +
      Object.entries(http.query)
        .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
        .join("&")
    : "";
  return `${vectors.base_url}${http.path}${query}`;
}

describe("HttpSubscriptionsClient — shared contract examples", () => {
  it("covers every example in the shared file", () => {
    expect(cases.length).toBeGreaterThanOrEqual(18);
    expect(new Set(cases.map((c) => c.client.op))).toEqual(
      new Set(["list", "create", "update", "remove"]),
    );
  });

  it.each(cases.map((c) => [c.name, c] as const))("%s", async (_name, c) => {
    const { fetch, calls } = fakeFetch(c.response);
    const client = new HttpSubscriptionsClient({
      baseUrl: vectors.base_url,
      getToken: async () => vectors.token,
      fetch,
    });
    const outcome = await call(client, c).then(
      (value) => ({ ok: true as const, value }),
      (error: unknown) => ({ ok: false as const, error }),
    );

    // 1. Exactly the example's request, once (no retries), identified only by the bearer token.
    expect(fetch).toHaveBeenCalledTimes(1);
    const sent = calls[0]!;
    expect(sent.url).toBe(expectedUrl(c.http));
    expect(sent.init.method).toBe(c.http.method);
    const headers = sent.init.headers as Record<string, string>;
    expect(headers["Authorization"]).toBe(`Bearer ${vectors.token}`);
    if (c.http.body === null) {
      expect(sent.init.body).toBeUndefined();
      expect(headers["Content-Type"]).toBeUndefined();
    } else {
      expect(headers["Content-Type"]).toBe("application/json");
      expect(JSON.parse(sent.init.body as string)).toEqual(c.http.body);
    }
    expect(JSON.stringify(sent)).not.toMatch(/@example\.com/); // never an email in the request

    // 2. Exactly what the response means.
    const { status, body } = c.response;
    if (status >= 400) {
      expect(outcome.ok).toBe(false);
      const err = (outcome as { error: unknown }).error;
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(status);
      expect((err as ApiError).code).toBe(body?.["error"]);
      expect((err as ApiError).details).toEqual(body?.["details"] ?? {});
    } else {
      expect(outcome).toEqual({ ok: true, value: c.client.op === "remove" ? undefined : body });
    }
  });

  it("reads the example values the page relies on", async () => {
    const list = cases.find((c) => c.name === "list_mixed")!;
    const { fetch } = fakeFetch(list.response);
    const client = new HttpSubscriptionsClient({
      baseUrl: vectors.base_url,
      getToken: async () => vectors.token,
      fetch,
    });
    const out = await client.list();
    const byId = Object.fromEntries(out.subscriptions.map((s) => [s.id, s]));
    expect(out.limits).toEqual({ max: 10, used: 5 });
    expect(out.months.current).toBe("2026-11");
    expect(byId["TPE-TYO"]).toMatchObject({
      month_mode: "rolling",
      departure_month: null,
      plan_name: "tokyo",
      effective_month: "2026-12",
      status: "active",
    });
    expect(byId["TPE-TYO"]!.version).toMatch(/^legacy-[0-9a-f]{32}$/);
    expect(byId["TPE-SEL"]).toMatchObject({ created_at: null, updated_at: null });
    expect(byId["TPE-OSA_2027-01"]).toMatchObject({
      version: vectors.rows.fixed_jan.version_token,
      month_mode: "fixed",
      status: "active",
      fare_check: null,
    });
    expect(byId["KHH-OKA_2027-02"]!.status).toBe("paused");
    expect(byId["RMQ-SEL_2026-10"]!.status).toBe("expired");
    expect(byId["TPE-PAR_2027-01"]).toBeUndefined(); // another user's row is never listed
  });
});
