import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import tsConfigPaths from "vite-tsconfig-paths";
import {
  assertHttpModeLoopback,
  m14HttpAllowed,
  m14PrototypeEnabled,
} from "./src/lib/prototype-flag";

// M1.4 Phase 3B-C: refuse a network-exposed dev server while the local HTTP mode is requested.
function m14HttpLoopbackGuard(apiMode: string | undefined): Plugin {
  return {
    name: "m14-http-loopback-guard",
    configResolved(config) {
      if (config.command === "serve") assertHttpModeLoopback(apiMode, config.server.host);
    },
  };
}

// Plain client-side SPA: `vite build` emits static files to dist/.
// Deep links (/app, /signin, ...) are resolved by React Router in the browser;
// the host must fall back to index.html (see vercel.json).
export default defineConfig(({ command, mode }) => {
  const vercelEnv = process.env["VERCEL_ENV"];
  const httpAllowed = m14HttpAllowed({ command, vercelEnv });
  const apiMode = loadEnv(mode, process.cwd(), "VITE_")["VITE_M14_API_MODE"];
  return {
    plugins: [react(), tailwindcss(), tsConfigPaths(), m14HttpLoopbackGuard(apiMode)],
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
        m14PrototypeEnabled({ command, vercelEnv }) ? "true" : "false",
      ),
      // M1.4 Phase 3B-C local HTTP mode: "true" only on the dev server. In every build the
      // switch and its settings are replaced by constants, so the HTTP client, any harness URL
      // and any development token are compiled out whatever the environment says.
      "import.meta.env.VITE_M14_HTTP_ALLOWED": JSON.stringify(httpAllowed ? "true" : "false"),
      ...(httpAllowed
        ? {}
        : {
            "import.meta.env.VITE_M14_API_MODE": JSON.stringify(""),
            "import.meta.env.VITE_M14_API_URL": JSON.stringify(""),
            "import.meta.env.VITE_M14_DEV_TOKEN": JSON.stringify(""),
          }),
    },
  };
});
