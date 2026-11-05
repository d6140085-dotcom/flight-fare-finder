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
