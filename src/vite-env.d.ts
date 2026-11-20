/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_SUPABASE_URL: string;
  readonly VITE_SUPABASE_PUBLISHABLE_KEY: string;
  readonly VITE_FLIGHT_API_URL: string;
  /** "true" only in dev-server and Vercel preview builds (vite.config.ts); otherwise no M1.4 prototype. */
  readonly VITE_M14_PROTOTYPE?: string;
  /** Phase 3B-C local HTTP mode. "true" only on the Vite dev server (vite.config.ts). */
  readonly VITE_M14_HTTP_ALLOWED?: string;
  /** "http" to use the local harness on the dev server; anything else: the mock client. */
  readonly VITE_M14_API_MODE?: string;
  /** Localhost URL of the Phase 3B-B harness. Blank in every build. */
  readonly VITE_M14_API_URL?: string;
  /** Fake harness token (harness-token-user|other|third). Dev server only; blank in every build. */
  readonly VITE_M14_DEV_TOKEN?: string;
}
