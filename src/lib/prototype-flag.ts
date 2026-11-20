// Decides at build time whether the M1.4 prototype route is compiled in. Fail-closed:
// - Vercel production: never, whatever any prototype-related variable says.
// - Vite dev server (`vite` / `vercel dev`): yes.
// - Vercel preview deployments (VERCEL_ENV=preview, i.e. authorized branch previews): yes.
// - Anything else, including an unset or unknown VERCEL_ENV (local `vite build`, other hosts,
//   custom Vercel environments): no.
export interface BuildContext {
  command: "serve" | "build";
  vercelEnv: string | undefined;
}

export function m14PrototypeEnabled({ command, vercelEnv }: BuildContext): boolean {
  if (vercelEnv === "production") return false;
  if (command === "serve") return true;
  return vercelEnv === "preview";
}

// ---- M1.4 Phase 3B-C: local HTTP mode (fail-closed) ----------------------------------------
// The HTTP subscriptions client may run only on the local Vite dev server. Every `vite build`
// (Vercel production and preview included) compiles it out, whatever any VITE_ variable says.

/** True only on the Vite dev server, and never on Vercel production or preview builds. */
export function m14HttpAllowed({ command, vercelEnv }: BuildContext): boolean {
  if (vercelEnv === "production" || vercelEnv === "preview") return false;
  return command === "serve";
}

/** Loopback-only dev-server bindings. `undefined` is Vite's default (`localhost`). */
export function isLoopbackHost(host: string | boolean | undefined): boolean {
  if (host === undefined) return true;
  if (typeof host !== "string") return false; // `--host` with no value means all interfaces
  return ["localhost", "127.0.0.1", "::1", "[::1]"].includes(host);
}

/**
 * Refuses to start the dev server in HTTP mode on a non-loopback interface, so the local
 * harness mode (and its fake development token) is never reachable from the network.
 */
export function assertHttpModeLoopback(
  apiMode: string | undefined,
  host: string | boolean | undefined,
): void {
  if (apiMode === "http" && !isLoopbackHost(host)) {
    throw new Error(
      "VITE_M14_API_MODE=http is allowed only on a loopback dev server (localhost / 127.0.0.1 / ::1); " +
        "remove --host or the server.host setting.",
    );
  }
}
