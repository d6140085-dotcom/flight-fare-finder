// Chooses the subscriptions client (fail-closed). MockSubscriptionsClient is the default.
//
// HttpSubscriptionsClient (Phase 3B-C) is used only when ALL of these hold:
//   1. the build is the local Vite dev server: VITE_M14_HTTP_ALLOWED is "true", which vite.config
//      sets from the build command; every `vite build` (production and Vercel preview) defines
//      it as "false" and blanks the three settings below, so the HTTP branch is compiled out;
//   2. VITE_M14_API_MODE is exactly "http";
//   3. VITE_M14_API_URL is an http(s) URL on a loopback host (the Phase 3B-B harness);
//   4. VITE_M14_DEV_TOKEN is one of the harness's fake tokens.
// Anything else in HTTP mode throws ClientUnavailable instead of quietly showing mock data.
// VITE_FLIGHT_API_URL (the production M1 API) is never read here, so there is no fallback to it.
//
// The development token is a stand-in for local UI-to-harness integration only. It is never a
// Supabase token, the Supabase session is never read in HTTP mode, and it says nothing about
// production authentication (which the API verifies with Supabase).
import type { SubscriptionsClient } from "./api";
import { HttpSubscriptionsClient } from "./http-client";
import { MockSubscriptionsClient, type MockOptions } from "./mock-client";
import { mockDataAllowed } from "./routes";

export class ClientUnavailable extends Error {
  constructor(reason = "No subscriptions API client is configured for this build.") {
    super(reason);
    this.name = "ClientUnavailable";
  }
}

/** The fake bearer tokens of the Phase 3B-B local harness. Nothing else is accepted. */
export const DEV_TOKENS: ReadonlySet<string> = new Set([
  "harness-token-user",
  "harness-token-other",
  "harness-token-third",
]);

const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);

/**
 * Build-time constant. False in every `vite build` (VITE_M14_HTTP_ALLOWED is defined as "false"
 * and MODE is not "test"), so the minifier drops the HTTP branch and the HTTP client with it.
 * True on the dev server and in the test runner (where tests inject the environment).
 */
export const HTTP_COMPILED_IN =
  import.meta.env.VITE_M14_HTTP_ALLOWED === "true" || import.meta.env.MODE === "test";

export interface ClientEnv {
  httpAllowed?: string | undefined;
  apiMode?: string | undefined;
  apiUrl?: string | undefined;
  devToken?: string | undefined;
}

export type ClientConfig =
  { kind: "mock" } | { kind: "http"; baseUrl: string; devToken: string; host: string };

function buildEnv(): ClientEnv {
  return {
    httpAllowed: import.meta.env.VITE_M14_HTTP_ALLOWED,
    apiMode: import.meta.env.VITE_M14_API_MODE,
    apiUrl: import.meta.env.VITE_M14_API_URL,
    devToken: import.meta.env.VITE_M14_DEV_TOKEN,
  };
}

/** Decides mock or local HTTP mode; throws ClientUnavailable for any unsafe HTTP setting. */
export function resolveClientConfig(env: ClientEnv = buildEnv()): ClientConfig {
  if (!HTTP_COMPILED_IN) return { kind: "mock" }; // every build: HTTP compiled out
  if (env.httpAllowed !== "true") return { kind: "mock" };
  const mode = env.apiMode ?? "";
  if (mode === "" || mode === "mock") return { kind: "mock" };
  if (mode !== "http") throw new ClientUnavailable(`Unknown VITE_M14_API_MODE "${mode}".`);

  let url: URL;
  try {
    url = new URL(env.apiUrl ?? "");
  } catch {
    throw new ClientUnavailable("HTTP mode needs VITE_M14_API_URL (a localhost harness URL).");
  }
  if (
    (url.protocol !== "http:" && url.protocol !== "https:") ||
    !LOOPBACK.has(url.hostname) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) {
    throw new ClientUnavailable("HTTP mode only talks to a localhost harness (VITE_M14_API_URL).");
  }
  const token = env.devToken ?? "";
  if (!DEV_TOKENS.has(token)) {
    throw new ClientUnavailable(
      "HTTP mode needs VITE_M14_DEV_TOKEN set to a local harness token (harness-token-user, -other or -third).",
    );
  }
  return { kind: "http", baseUrl: url.href.replace(/\/+$/, ""), devToken: token, host: url.host };
}

export function createSubscriptionsClient(
  opts: MockOptions & { allowMock?: boolean; env?: ClientEnv } = {},
): SubscriptionsClient {
  const config = resolveClientConfig(opts.env);
  if (HTTP_COMPILED_IN && config.kind === "http") {
    const token = config.devToken;
    // The fake token goes only to the validated localhost URL; the Supabase session is not read.
    return new HttpSubscriptionsClient({ baseUrl: config.baseUrl, getToken: async () => token });
  }
  const allowMock = opts.allowMock ?? mockDataAllowed();
  if (!allowMock) throw new ClientUnavailable();
  return new MockSubscriptionsClient(opts);
}
