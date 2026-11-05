import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import tsConfigPaths from "vite-tsconfig-paths";
import { m14PrototypeEnabled } from "./src/lib/prototype-flag";

// Plain client-side SPA: `vite build` emits static files to dist/.
// Deep links (/app, /signin, ...) are resolved by React Router in the browser;
// the host must fall back to index.html (see vercel.json).
export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss(), tsConfigPaths()],
  resolve: {
    dedupe: ["react", "react-dom", "react-router"],
  },
  // Pre-bundle deps up front so a mid-session re-optimization can't load two React copies.
  optimizeDeps: {
    include: [
      "sonner",
      "@radix-ui/react-label",
      "@radix-ui/react-slot",
      "lucide-react",
      "@supabase/supabase-js",
    ],
  },
  build: {
    outDir: "dist",
  },
  // M1.4 prototype page, fail-closed (src/lib/prototype-flag.ts): dev server and Vercel preview
  // builds only. This define always overrides any VITE_M14_PROTOTYPE set in the environment.
  define: {
    "import.meta.env.VITE_M14_PROTOTYPE": JSON.stringify(
      m14PrototypeEnabled({ command, vercelEnv: process.env["VERCEL_ENV"] }) ? "true" : "false",
    ),
  },
}));
