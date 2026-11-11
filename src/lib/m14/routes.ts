// M1.4 prototype (mock data only). Fail-closed: the routes exist only when the build set
// VITE_M14_PROTOTYPE to exactly "true" (dev server and Vercel preview builds; see
// src/lib/prototype-flag.ts and vite.config.ts). Unset or any other value means no route.
export const M14_PROTOTYPE_ENABLED = import.meta.env.VITE_M14_PROTOTYPE === "true";
/** Phase 1A/3A preview without login (mock data). */
export const M14_PROTOTYPE_PATH = "prototype/subscriptions";
/** Phase 3A: the signed-in subscription workflow against the in-memory mock API. */
export const M14_APP_PATH = "app/subscriptions";
