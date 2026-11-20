// Phase 3B-C: the fail-closed client switch. Mock by default; the HTTP client only with every
// guard satisfied; any unsafe HTTP setting is ClientUnavailable, never a silent fallback.
import { vi } from "vitest";

const { getAccessToken } = vi.hoisted(() => ({
  getAccessToken: vi.fn(async () => "real-supabase-session-token"),
}));
vi.mock("@/lib/flight-api", () => ({ getAccessToken }));

import {
  ClientUnavailable,
  DEV_TOKENS,
  createSubscriptionsClient,
  resolveClientConfig,
  type ClientEnv,
} from "./client-factory";
import { HttpSubscriptionsClient } from "./http-client";
import { MockSubscriptionsClient } from "./mock-client";

const OK: ClientEnv = {
  httpAllowed: "true",
  apiMode: "http",
  apiUrl: "http://127.0.0.1:8787",
  devToken: "harness-token-user",
};

describe("resolveClientConfig", () => {
  it("is mock by default and whenever the build has no HTTP switch", () => {
    expect(resolveClientConfig({})).toEqual({ kind: "mock" });
    for (const httpAllowed of [undefined, "", "false", "TRUE", "1"]) {
      expect(resolveClientConfig({ ...OK, httpAllowed })).toEqual({ kind: "mock" });
    }
    for (const apiMode of [undefined, "", "mock"]) {
      expect(resolveClientConfig({ ...OK, apiMode })).toEqual({ kind: "mock" });
    }
  });

  it("selects HTTP only when all four guards hold", () => {
    expect(resolveClientConfig(OK)).toEqual({
      kind: "http",
      baseUrl: "http://127.0.0.1:8787",
      devToken: "harness-token-user",
      host: "127.0.0.1:8787",
    });
    for (const apiUrl of [
      "http://localhost:8787/",
      "https://127.0.0.1:9443",
      "http://[::1]:8787",
    ]) {
      expect(resolveClientConfig({ ...OK, apiUrl }).kind).toBe("http");
    }
    for (const devToken of DEV_TOKENS) {
      expect(resolveClientConfig({ ...OK, devToken }).kind).toBe("http");
    }
  });

  it.each([
    "https://vgc6ctabv7.execute-api.ap-southeast-2.amazonaws.com",
    "https://flight-fare-finder-red.vercel.app",
    "https://abcdefghijklmnop.supabase.co",
    "http://192.168.1.20:8787",
    "http://0.0.0.0:8787",
    "http://localhost.evil.example:8787",
    "http://127.0.0.1.nip.io:8787",
    "http://user:pw@127.0.0.1:8787",
    "http://127.0.0.1:8787?x=1",
    "ftp://127.0.0.1:8787",
    "127.0.0.1:8787",
    "",
    undefined,
  ])("rejects the non-local or malformed API URL %s", (apiUrl) => {
    expect(() => resolveClientConfig({ ...OK, apiUrl })).toThrow(ClientUnavailable);
  });

  it.each([
    undefined,
    "",
    "harness-token-expired",
    "harness-token-admin",
    "harness-token-user ",
    "HARNESS-TOKEN-USER",
    "test-access-token",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.sig", // a JWT, like a Supabase session
    "sb_publishable_realLookingKey123",
    "sb_secret_abcdef",
  ])("fails closed for the development token %s", (devToken) => {
    expect(() => resolveClientConfig({ ...OK, devToken })).toThrow(ClientUnavailable);
  });

  it("fails closed for an unknown mode instead of guessing", () => {
    for (const apiMode of ["HTTP", "https", "prod", "live"]) {
      expect(() => resolveClientConfig({ ...OK, apiMode })).toThrow(ClientUnavailable);
    }
  });

  it("never falls back to VITE_FLIGHT_API_URL (the production M1 API)", () => {
    const env = { ...OK, apiUrl: undefined, VITE_FLIGHT_API_URL: "http://127.0.0.1:9999" };
    expect(() => resolveClientConfig(env as ClientEnv)).toThrow(ClientUnavailable);
  });
});

describe("createSubscriptionsClient", () => {
  beforeEach(() => getAccessToken.mockClear());

  it("builds the mock client by default", () => {
    expect(createSubscriptionsClient()).toBeInstanceOf(MockSubscriptionsClient);
    expect(createSubscriptionsClient({ env: {} })).toBeInstanceOf(MockSubscriptionsClient);
  });

  it("builds the HTTP client only with every guard, sending only the dev token to localhost", async () => {
    const calls: { url: string; auth: string | null }[] = [];
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      calls.push({
        url: String(url),
        auth: new Headers(init?.headers as HeadersInit).get("Authorization"),
      });
      return new Response(
        JSON.stringify({
          subscriptions: [],
          limits: { max: 10, used: 0 },
          months: { current: "2026-11", selectable: ["2026-12"] },
        }),
        { status: 200 },
      );
    });
    try {
      const client = createSubscriptionsClient({ env: OK });
      expect(client).toBeInstanceOf(HttpSubscriptionsClient);
      expect(client.kind).toBe("http");
      await client.list();
      expect(calls).toEqual([
        { url: "http://127.0.0.1:8787/subscriptions", auth: "Bearer harness-token-user" },
      ]);
      expect(getAccessToken).not.toHaveBeenCalled(); // the Supabase session is never read
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it("throws ClientUnavailable rather than showing mock data for a bad HTTP setting", () => {
    expect(() =>
      createSubscriptionsClient({
        env: { ...OK, apiUrl: "https://vgc6ctabv7.execute-api.ap-southeast-2.amazonaws.com" },
      }),
    ).toThrow(ClientUnavailable);
    expect(() => createSubscriptionsClient({ env: { ...OK, devToken: "x" } })).toThrow(
      ClientUnavailable,
    );
  });

  it("still refuses mock data where mock data is not allowed", () => {
    expect(() => createSubscriptionsClient({ allowMock: false, env: {} })).toThrow(
      ClientUnavailable,
    );
  });
});
