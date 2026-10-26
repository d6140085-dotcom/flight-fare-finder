---
name: lovable-best-practice
description: Hard rules and workflow tips for working with Lovable in the Flight Price Notifier course. Use whenever a student is generating, re-rolling, or editing the M0 Lovable landing page — applies to any Lovable session in the course. Sourced from a real production Lovable + Supabase project.
---

# Lovable Best Practice (Flight Price Notifier)

This skill is loaded any time the student is interacting with Lovable — which in this course is **M0** (the landing page + sign-in) and any later re-roll of the front-end. It lives at two levels:

1. **Hard rules** — non-negotiable, learned from a real production Lovable + Supabase project. Each rule has a real incident behind it. Skipping these costs the student hours later.
2. **General workflow tips** — Vercel framework preset gotchas, re-roll discipline, git commit habits.

When you (Claude Code) are guiding a student through Lovable steps, **apply these rules proactively** — don't wait for the student to ask. If you see the student about to break one, stop them and explain why.

> **Architecture note that shapes these rules:** in *this* course **Supabase is AUTH ONLY** — there are **no custom Supabase tables, no RPCs, no client-side data queries**. All application data lives in **DynamoDB on AWS** (from M1.1), reached only by Lambdas. So the original project's "reuse RPCs / use RPC for cross-table queries" rules **do not apply here** — they're replaced below by the rules that actually bite in an auth-only Lovable build.

---

## Execution mode: Cowork vs CLI

Lovable itself works the same way in both (it's a hosted web app), but the **verification + git steps around Lovable** differ.

| Operation | CLI mode | Cowork mode |
|---|---|---|
| Confirm a Lovable iteration synced to GitHub | `gh api repos/<owner>/<repo>/commits --jq '.[0].commit.message'` | GitHub MCP, Lovable's built-in Git panel, or open `https://github.com/<owner>/<repo>/commits` |
| PII grep over changed files (Rule 3) | `grep -rE '@(gmail\|yahoo\|hotmail\|outlook)\.com' .` locally | No local checkout — review the Lovable diff in the Lovable UI before saving, or open the diff in GitHub web after it pushes |
| Edit Lovable's generated files directly | Claude Code Edit/Write on a local clone | Edit through Lovable's own editor; there's no local working copy |

The **hard rules** apply identically in both modes — only the verification mechanics change. Wherever this skill says `gh ...`, treat that as **CLI-only** and substitute the Cowork equivalent.

---

## Hard rules (apply to every Lovable session in this course)

### Rule 1 — Do NOT connect Supabase (or GitHub) on the first Lovable prompt

> **The rule:** In M0, create the Lovable project with a blank placeholder prompt first, get to a working preview, and only connect **GitHub in Step 3** and **Supabase in Step 5** — never on the initial generation.

**Why:** Connecting Supabase too early makes Lovable scaffold auth against a backend before the page even exists, and the cleanup (re-pointing to the student's own Supabase) gets tangled with the initial generation. Connecting late means the swap is a single clean step. This is also why M0 uses Lovable's *default* auth backend for v1 and swaps to the student's own Supabase only in Step 5.

**How to apply:** If the student (or Lovable) tries to "Connect Supabase" during Step 1–2, stop them: 「先不要連 Supabase，我們在 Step 5 一次接乾淨。」

---

### Rule 2 — Supabase is AUTH ONLY — block any custom table / RPC Lovable proposes

> **The rule:** The only Supabase objects this product uses are the built-in `auth.users` (sign-up/sign-in). If Lovable offers to create a `profiles` table, a `subscriptions` table, an RPC, or any client-side `.from('…').select()`, **say no.**

**Why:** All application data (the flight subscriptions) lives in **DynamoDB on AWS**, written only by Lambdas behind API Gateway — the browser never queries app data directly. A Supabase data table created by Lovable would become a shadow source of truth that diverges from DynamoDB and confuses students about where data lives. (It also drags in Supabase RLS, which silently filters rows and eats debugging hours — a problem we simply avoid by not storing app data there.)

**How to apply:** When reviewing a Lovable diff, watch for `supabase.from(...)`, new `.sql` migrations, or "create table" prompts. Block them and remind the student: 「資料一律走 AWS（M1.1 之後的 DynamoDB），Supabase 只做登入。」

---

### Rule 3 — Never commit PII (email, real names, addresses) in code or test data

> **The rule:** When Lovable generates demo data, mock users, or feature copy with example emails / names, replace them with obvious fakes (`user@example.com`, `Test User`) before committing.

**Why:** Lovable sometimes auto-fills demo data with whatever email is signed into your account, or copies a real-looking email from your prompt. Once that hits the GitHub repo it's in git history forever — even after deletion, anyone with a clone has it. For a course product you'll share publicly, that's a trust/compliance issue.

**How to apply:** Before any Lovable-triggered commit, scan changed files:
- **CLI:** `grep -rE '@(gmail|yahoo|hotmail|outlook)\.com' .`
- **Cowork:** review the diff in Lovable's UI before saving, or scan the just-pushed commit in GitHub web.

Swap any real-looking emails to `example.com` placeholders first. (Note: the student's *own* test sign-up email living in **Supabase auth.users** is fine — that's runtime auth data, not committed code.)

---

### Rule 4 — Filenames use PascalCase for components

> **The rule:** React component files must be `PascalCase.tsx` — e.g. `SubscribeForm.tsx`, `RouteCard.tsx`. Not `subscribe-form.tsx`, not `subscribeForm.tsx`.

**Why:** Lovable's default file-naming is inconsistent (kebab/camel/Pascal, sometimes mixed in one project). Once the project grows, finding `<SubscribeForm />` among `subscribe-form.tsx` / `subscribeForm.tsx` / `SubscribeForm.tsx` is a real tax. Pick one and tell Lovable to enforce it.

**How to apply:**
- Component files (export a React component): `PascalCase.tsx`.
- Non-component files (hooks, utils, types): keep Lovable's default (`useAuthState.ts`).
- If Lovable makes a kebab-case `.tsx`, tell it: "rename to PascalCase to match convention."

---

## General Lovable workflow tips

### Tip 1 — Vercel framework preset auto-detection sometimes picks wrong

Lovable's output is Vite + React. Vercel usually auto-detects that, but ~10% of the time it picks "Other" / guesses Next.js and the deploy fails with a cryptic build error.

**If a Vercel deploy fails right after Lovable connects to GitHub (M0 Step 4):** Vercel project → Settings → General → Framework Preset → set to **Vite** → redeploy.

### Tip 2 — Free-tier credit budget — re-roll discipline

Lovable's free tier limits generations per day (verify current quota in your account). Each "fix and regenerate" eats one; students who don't think before prompting can burn the daily budget in 15 minutes and be stuck for 24 hours.

**Discipline:**
- First prompt = the **full** prompt verbatim from `m0-landing-and-signin` Step 2. Don't try shorter versions first.
- Small issues (color, copy) → use Lovable's edit-in-place, not a full regenerate.
- If you must regenerate, batch *all* desired changes into one follow-up prompt — not 3 small re-rolls.

### Tip 3 — Always commit to GitHub before iterating in Lovable

Lovable two-way-syncs with GitHub once connected (M0 Step 3). If a later generation overwrites something you liked, you want a commit to roll back to.

**Habit:** after every Lovable iteration the student is happy with, confirm the GitHub repo has a matching commit:
- **CLI:** `gh api repos/<owner>/<repo>/commits --jq '.[0].commit.message'`
- **Cowork:** open `https://github.com/<owner>/<repo>/commits`, or use the GitHub MCP / Lovable's Git panel.

---

## What this skill is NOT

- Not a generic Lovable tutorial — it's opinionated guidance for **this** course (Flight Price Notifier).
- Not a replacement for `m0-landing-and-signin` — that skill has the step-by-step; this provides the *rules* it assumes.
- Not Lovable documentation — see https://docs.lovable.dev.

## Source

- Hard rules trace to a real production multi-page Lovable + Supabase project's Lovable notes. Adapted here for the **auth-only** Supabase usage of this course (the original RPC/cross-table rules are replaced by the "Supabase is auth-only" rules above).
- Related: [[supabase-best-practice]] (auth-only Supabase rules), [[aws-best-practice]] (where the data actually lives).

## TODO (fill in after running M0 a few times)

- [ ] Confirm the current Lovable free-tier generation limit.
- [ ] Capture any new M0 pitfalls from the first cohort.
- [ ] Add Lovable prompt wording that reliably yields v1 with a working Supabase-auth sign-in on the first try.
