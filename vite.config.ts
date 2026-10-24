import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import tsConfigPaths from "vite-tsconfig-paths";

// Plain client-side SPA: `vite build` emits static files to dist/.
// Deep links (/app, /signin, ...) are resolved by React Router in the browser;
// the host must fall back to index.html (see vercel.json).
export default defineConfig({
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
});
