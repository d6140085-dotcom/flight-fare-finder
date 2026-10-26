# Flight Price Notifier — project rules

These rules override any course skill (including later-milestone skills) that assumes otherwise.

## Architecture

Claude + GitHub + Vercel + Supabase Auth + AWS.

- **Front end:** static Vite + React SPA with React Router (`/`, `/app`, `/signin`, `/signup`).
  `vite build` → `dist/`; `vercel.json` rewrites every path to `/index.html`.
- **Hosting:** Vercel, with env vars `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY`.
- **Auth:** Supabase project `flight-price-notifier-001` (ref `ftkcnrtlgwrpvjrszidu`), **auth only**:
  no custom tables, RPCs or client-side data queries. Publishable key only in the front end;
  never a service-role key.
- **App data (later milestones):** AWS (DynamoDB via Lambdas), not Supabase.
- **Lovable is deprecated.** Don't suggest Lovable prompts, re-rolls or sync checks.

## GitHub credential

- The PAT is in AWS SSM Parameter Store at `/github/pat` (SecureString, `ap-southeast-2`).
- It is used only inside the approved `github-api-bridge` Lambda (`ap-southeast-2`,
  payload `{"endpoint": "/..."}`).
- Never retrieve, print, log or return the token. Don't ask the owner for it, and don't copy it
  into Secrets Manager unless explicitly instructed.

## Git workflow

- Never push to `main`.
- Every change: feature branch → push the branch → open a PR against `main`.
- Never merge the PR yourself; the owner merges.
