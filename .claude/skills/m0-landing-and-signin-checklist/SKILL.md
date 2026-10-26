---
name: m0-landing-and-signin-checklist
description: Flight Price Notifier Milestone 0 verification — checks every artifact (GitHub repo, Lovable sync, Vercel deploy, landing page contents, Supabase auth) is real and correctly wired. Use when the student says "驗收 M0", "check M0", "M0 done?", or after the `m0-landing-and-signin` skill completes Step 5.
---

# M0 — Landing + Sign-in Checklist

## What this skill does

Verifies the student actually completed M0 — not just *thinks* they did. People (and LLMs) skip steps. This skill tests every artifact and reports pass/fail per item, then emits a `READY for M1.1` verdict.

**Run this AFTER `m0-landing-and-signin` Step 5, or any time the student claims M0 is done.**

## Execution mode: Cowork vs CLI (read this first)

| Section | CLI mode tool | Cowork mode equivalent |
|---|---|---|
| A — GitHub repo | `gh repo view` / `gh api` | GitHub MCP, or open repo URL in browser |
| B — Vercel deploy | `curl` | `mcp__vercel__*`, or open URL in browser |
| C — Landing page contents | `curl … \| grep` | Playwright MCP, or student inspects in browser |
| D — Supabase auth | `supabase` CLI / dashboard | Supabase MCP (preferred both modes) |

In Cowork mode every Bash block below is CLI-only — use the equivalent. Don't try to install `gh`/`curl` in Cowork.

## How to run

The student invokes this directly (e.g. types `驗收 M0`). You (Claude Code) **actively run** each check via Bash and report results — don't just describe them.

### Step 1: Collect URLs (one message)

Ask the student for:
1. GitHub repo URL (`https://github.com/<user>/<repo>`)
2. Vercel deploy URL (`https://<app>.vercel.app`)
3. Supabase project URL (`https://<ref>.supabase.co`)

### Step 2: Run the checklist

#### Section A — GitHub repo
- **A1** Repo exists and has Lovable's files:
  ```bash
  gh repo view <owner>/<repo> --json name,visibility,defaultBranchRef
  gh api repos/<owner>/<repo>/contents | grep -oE '"name": "[^"]+"' | head
  ```
- **A2** Recent commit from the Lovable→GitHub sync:
  ```bash
  gh api repos/<owner>/<repo>/commits --jq '.[0].commit.message' | head -1
  ```
  *Recovery if missing:* re-connect GitHub in Lovable (M0 Step 3).

#### Section B — Vercel deploy
- **B1** Live URL returns 200:
  ```bash
  curl -sS -o /dev/null -w "%{http_code}\n" https://<app>.vercel.app
  ```
- **B2** It's auto-deploying from GitHub (push → redeploy). Confirm in Vercel dashboard the project's Git connection points at the repo from A1.
  *Recovery:* re-import the repo on vercel.com/new (M0 Step 8).
- **B3** **Deep link works (the SSR-vs-SPA trap):** `/app` does NOT 404:
  ```bash
  curl -sS -o /dev/null -w "%{http_code}\n" https://<app>.vercel.app/app
  ```
  Expect `200` (or a redirect to sign-in if unauthenticated — both are fine; a **404** means the app shipped as SSR without a SPA fallback). *Recovery:* M0 Step 7 — convert to a plain Vite SPA, re-deploy.

#### Section C — Landing page contents
- **C1** Hero + feature cards + Sign In/Up button present:
  ```bash
  curl -sS https://<app>.vercel.app | grep -oiE "登入|sign in|sign up|機票|目標價|追蹤" | sort | uniq -c
  ```
  (Note: a Vite/React SPA may render client-side — if grep is empty, fall back to a browser/Playwright check.)
- **C2** Browser check (Cowork or if C1 empty): open the URL, confirm hero headline 「設定航線與目標價，機票降價就通知你」, three cards, and a Sign In/Up button are visible.

#### Section D — Supabase auth
- **D1** The student's OWN Supabase project has Email auth enabled (Authentication → Providers).
- **D2** **The decisive test:** sign up a brand-new test email on the live site, then check Supabase → Authentication → Users — the new user appears in the student's project (not a Lovable-default backend).
  ```bash
  # If supabase CLI is linked to the project:
  supabase projects list
  ```
- **D3** Sign-in AND sign-out both work on the live site (close the loop).
  *Recovery:* redo M0 Step 9 (swap auth to the student's Supabase) — common miss is leaving Lovable's default backend in place.

## Reporting

Emit a table:

| Check | Status | Notes |
|---|---|---|
| A1 repo exists | ✅ / ❌ | |
| A2 Lovable sync commit | ✅ / ❌ | |
| B1 Vercel 200 | ✅ / ❌ | |
| B2 auto-deploy wired | ✅ / ❌ | |
| B3 /app deep-link not 404 | ✅ / ❌ | SSR-vs-SPA trap |
| C1/C2 landing contents | ✅ / ⚠️ / ❌ | |
| D1 email auth enabled | ✅ / ❌ | |
| D2 new user in student's Supabase | ✅ / ❌ | the key one |
| D3 sign-in + sign-out loop | ✅ / ❌ | |

**Verdict:**
- All ✅ → 「M0 驗收通過 ✅ READY for M1.1。跟我說『啟動 M1.1』，我們來讓使用者真的訂閱一條航線。」
- Any ❌ → list the failed items + the recovery step, and tell the student to fix then re-run `驗收 M0`.
