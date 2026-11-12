// M1.4 prototype (mock data only). Fail-closed: the routes exist only when the build set
// VITE_M14_PROTOTYPE to exactly "true" (dev server and Vercel preview builds; see
// src/lib/prototype-flag.ts and vite.config.ts). Unset or any other value means no route.
export const M14_PROTOTYPE_ENABLED = import.meta.env.VITE_M14_PROTOTYPE === "true";
/** Phase 1A/3A preview without login (mock data). */
export const M14_PROTOTYPE_PATH = "prototype/subscriptions";
/** Phase 3A: the signed-in subscription workflow against the in-memory mock API. */
export const M14_APP_PATH = "app/subscriptions";

/**
 * True where in-memory mock data may be used: builds with the M1.4 preview flag (dev server,
 * Vercel previews) and the test runner. Production builds compile the flag to "false".
 */
export function mockDataAllowed(env: { flag?: string | undefined; mode?: string } = {}): boolean {
  const flag = "flag" in env ? env.flag : import.meta.env.VITE_M14_PROTOTYPE;
  const mode = env.mode ?? import.meta.env.MODE;
  return flag === "true" || mode === "test";
}
