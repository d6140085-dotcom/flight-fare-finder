/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_SUPABASE_URL: string;
  readonly VITE_SUPABASE_PUBLISHABLE_KEY: string;
  readonly VITE_FLIGHT_API_URL: string;
  /** "false" in Vercel production builds (set in vite.config.ts); hides the M1.4 prototype. */
  readonly VITE_M14_PROTOTYPE?: string;
}
