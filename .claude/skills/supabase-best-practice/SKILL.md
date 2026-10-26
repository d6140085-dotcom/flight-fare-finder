---
name: supabase-best-practice
description: Hard rules for working with Supabase in the Flight Price Notifier course — where Supabase is used for AUTHENTICATION ONLY (no app data, no custom tables, no RPCs). Use whenever a student is wiring Supabase auth into the Lovable M0 page, handling Supabase keys, or asking where data should live. Sourced from a real production Lovable + Supabase project.
---

# Supabase Best Practice (Flight Price Notifier — AUTH ONLY)

> **Read this first — it changes everything below.** In this course **Supabase does exactly one job: authentication** (sign-up / sign-in / sign-out via the built-in `auth.users`). **No application data ever lives in Supabase** — every flight subscription lives in **DynamoDB on AWS**, written only by Lambdas. So the schema-migration / RPC / cross-table-query rules that dominate a *data-on-Supabase* project **do not apply here**. The rules that remain are about **keys, where-data-lives discipline, and the Lovable auth wiring** — and they matter a lot.

The original rules came from a real production Lovable + Supabase project where Supabase *was* the database. We keep only what's relevant to an auth-only build. When guiding a student, **apply these proactively** — stop them before they break one.

---

## Execution mode: Cowork vs CLI

Auth-only means almost no Supabase tooling is needed — mostly just creating the project and copying two values (URL + publishable/anon key) into Lovable. There's **no migration workflow and no local docker stack** in this course.

| Operation | CLI mode | Cowork mode |
|---|---|---|
| Confirm logged in | `supabase projects list` | Supabase MCP `list_projects` |
| Get URL / publishable key | `supabase projects api-keys --project-ref <ref>` / dashboard → Settings → API | Supabase MCP `get_project_url` + `get_publishable_keys` |
| Confirm a user signed up | dashboard → Authentication → Users | Supabase MCP `list_tables` won't show auth users — use the dashboard |

`supabase start` / `supabase db push` / local docker — **not used in this course** (no schema to manage).

---

## Hard rules

### Rule 1 — Supabase is AUTH ONLY — no custom tables, no RPCs, no client-side data queries

> **The rule:** The only Supabase objects this product uses are the built-in **`auth.users`**. Do not create a `profiles` table, a `subscriptions` table, an RPC, or any `supabase.from('…').select()` data query. If Lovable proposes one, say no.

**Why:** All application data (the flight subscriptions, the notification history) lives in **DynamoDB on AWS**, written only by Lambdas behind API Gateway. The browser never reads or writes app data directly. A Supabase data table would become a second, divergent source of truth and reintroduce exactly the class of bugs we designed the architecture to avoid — most notably **RLS silently filtering rows** (a `.from(a).select('*, b(*)')` join returns empty with *no error* when RLS denies either side; classic half-day debugging trap). We sidestep all of it by never putting app data in Supabase.

**How to apply:** If a student or Lovable wants to "store the subscription in Supabase" or "add a table," redirect: 「資料一律走 AWS — 訂閱寫進 DynamoDB（M1.1 的 `POST /subscribe` → Lambda）。Supabase 只負責登入。」

---

### Rule 2 — The publishable/anon key is the ONLY Supabase key in the front-end. The service-role key never leaves a server.

> **The rule:** The browser/Lovable/Vercel front-end uses **only** the Supabase **publishable key** (formerly "anon key", `sb_publishable_*` / legacy `ANON_KEY`). The **service-role / secret key** must never appear in client code, a Lovable prompt, a commit, or a screenshot.

**Why:** The publishable key is browser-safe by design (it can only do what Supabase auth + RLS allow). The service-role key **bypasses all security** — anyone who gets it owns every user's data. In a *data-on-Supabase* project the service-role key is needed server-side; in **this** course it's barely needed at all, because the servers that matter are **AWS Lambdas talking to DynamoDB via IAM**, not to Supabase. So if you ever see a service-role key being pasted anywhere in this project, that's almost certainly a mistake — stop it.

**How to apply:**
- M0 front-end / Lovable: publishable (anon) key only.
- If a student pastes a `service_role` key into Lovable, the front-end, or `.env` committed to git → stop them, explain the blast radius, and have them rotate it (Supabase dashboard → Settings → API → roll).
- Grep a deployed bundle for `service_role` → must be absent. (This is also an M3 go-live check.)
- **Caching the *publishable* key in the `flight/supabase` Secrets Manager secret is fine** (M1 prereq) — it's public-by-design and it's only there so a new Cowork session recalls the `url`+`publishable_key` without a dashboard hunt. **Never** cache the service-role key anywhere, AWS included.

---

### Rule 3 — Get auth wiring right in M0: student's OWN Supabase, email provider enabled, sign-OUT tested

> **The rule:** M0 must end with the live site authenticating against the **student's own** Supabase project (not Lovable's default backend), with the **Email** provider enabled, and the full **sign-up → sign-in → sign-out** loop verified.

**Why:** The #1 M0 miss is leaving Lovable's default auth backend in place — the student "signs up" but the user lands in Lovable's project, not theirs, and nothing carries forward. The #2 miss is email-confirmation turned on with no SMTP, so the test user can never finish sign-up. The #3 miss is never testing sign-out, which hides session bugs.

**How to apply (mirrors `m0-landing-and-signin` Step 5–6):**
1. Student creates their own Supabase project; copy **Project URL** + **publishable/anon key** into Lovable's Supabase connection (or the `SUPABASE_URL` / `SUPABASE_ANON_KEY` env vars).
2. Authentication → Providers → **Email enabled**. For the class demo, either disable email confirmation or use magic-link, so the test sign-up completes.
3. Verify: sign up a brand-new test email on the **live Vercel URL** → it appears in **the student's** Supabase → Authentication → Users. Then sign in and **sign out**.

---

## What this skill is NOT

- Not a Supabase database/migration/RLS guide — **this course doesn't use Supabase as a database**. (If a future course does, that's a different skill.)
- Not a Supabase tutorial — see https://supabase.com/docs.
- Not an auth-provider comparison — M0 uses email sign-in; social providers are an optional extension.

## Source

- Adapted from a real production Lovable + Supabase project's Supabase notes. That project used Supabase as its database, so its migration/RPC/cross-table rules were central there; **here Supabase is auth-only**, so those are dropped and only the key-hygiene + auth-wiring rules remain.
- Related: [[lovable-best-practice]] (the M0 Lovable build), [[aws-best-practice]] (where the data actually lives — DynamoDB).

## TODO

- [ ] Confirm the exact Lovable "Connect Supabase" UI flow for swapping to the student's project (M0 Step 5).
- [ ] Note the current Supabase default for email confirmation (on/off) so the M0 demo instruction stays accurate.
