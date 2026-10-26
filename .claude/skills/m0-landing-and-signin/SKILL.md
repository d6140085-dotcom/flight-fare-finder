---
name: m0-landing-and-signin
description: Flight Price Notifier Milestone 0 — generate the v1 landing page with Lovable, push to GitHub, set up the Cowork project + AWS/Vercel/Supabase connectors, cache the GitHub token in Secrets Manager, deploy to Vercel, and swap auth to the student's own Supabase. Use when the student says "啟動 M0", "start M0", "build the M0 landing page", "機票通知的入口網站", or any variant that maps to "先做出一個能登入的網站".
---

# M0 — Landing Page + Sign-in（先有一個能登入的機票通知網站）

## What this skill does

Walks the student through Milestone 0 end-to-end, in the order the course actually runs it: **Lovable → GitHub → make repo public → set up the Cowork project + connectors (AWS / Vercel / Supabase) → cache the GitHub token in Secrets Manager → convert to a Vite SPA & push → deploy to Vercel → swap auth to the student's own Supabase → bootstrap the skills & run the checklist.**

By the end the student has:

1. A v1 landing page (`/`) for **Flight Price Notifier** — a hero (「設定航線與目標價，機票降價就通知你」), three feature cards (盯緊熱門航線 / 達標自動通知 / 隨時取消), a top-right Sign in / 登入 button, a footer — plus an authenticated `/app` shell that greets the signed-in user and shows a "dashboard coming soon" placeholder.
2. A **GitHub repo** (created by Lovable) holding that code, made **public**.
3. A **Cowork desktop project** with **AWS API MCP**, **Vercel Connect**, and **Supabase Connector** installed — this is the workbench every later milestone uses.
4. The **GitHub PAT cached in AWS Secrets Manager**, so this and every later session **recall the same token to modify the repo** instead of re-pasting one.
5. A **live Vercel URL** that auto-deploys the repo on every push.
6. **Auth wired up** — register / log in / log out. v1 starts on **Lovable Cloud** (fastest path to a working sign-up), then **swaps to the student's own Supabase project**. Either way **auth is the ONLY thing Supabase does** — only the default `auth.users`; no app data ever lives in Supabase (subscriptions go to DynamoDB on AWS from M1.1 on).

**Out of scope for M0:** the subscribe UI, the DynamoDB `subscriptions` table, AWS Lambdas, the price-check loop, Resend email, and payments. Those are M1.1 and later. (Supabase never gets custom tables — all app data lives in DynamoDB on AWS.)

## When to load this skill

Trigger phrases:
- "啟動 M0" / "start M0" / "begin M0"
- "幫我蓋 M0 的 landing page"
- "機票通知的入口網站"
- Any prompt referencing "先做出一個能登入的網站"

Do NOT load this skill for M1.1+ — they have their own skills.

## Execution mode (Cowork-first)

This milestone is written for **Cowork on Desktop** — that's the course's primary environment, and Steps 5–6 set up the Cowork project and its connectors. The **one** part that needs a local shell is writing `~/.aws/credentials` (Step 5), and you hand the student a Claude Code CLI / desktop prompt for exactly that. Everywhere else, the AWS/Vercel/Supabase **connectors** do the work from inside Cowork.

If a student is on a pure local CLI instead, the `aws …` commands shown here apply directly (they're the same calls the AWS MCP makes); the connector-install bullets become "make sure `gh` / `vercel` / `aws` are authenticated locally."

## Required external accounts

| # | Service | Used for |
|---|---|---|
| 1 | Lovable (`lovable.dev`) | Generates the v1 UI + scaffolds auth |
| 2 | GitHub | Lovable creates a repo here; you make it public; Vercel imports from it. A **fine-grained PAT** (single repo, Contents: Write) gets cached in Secrets Manager (Step 6) |
| 3 | AWS | **Secrets Manager** holds the GitHub token (and, later, the Supabase values) so future sessions reuse them. Reached via the **AWS API MCP** connector (Cowork) or the `[default]` profile (CLI) |
| 4 | Vercel | Auto-deploys the GitHub repo; reached via the **Vercel Connect** connector |
| 5 | Supabase (`supabase.com`) | `auth.users`, sign up / sign in / sign out; reached via the **Supabase Connector** |

If any of these is missing, **stop and ask the student to register first.**

## The product being built

M0 builds **Flight Price Notifier** — fixed, no per-student variation. The v1 is a real, working signed-in SaaS: the landing page sells 「設定航線與目標價，機票降價就通知你」, and the Sign In / Sign Up button leads to a working auth flow. After signing in, the user lands on a placeholder authenticated page (e.g. 「Hi {email}，你的航線追蹤儀表板即將上線」). The subscribe form and price engine come in M1.1+.

## Architecture

![Flight Fare Checker architecture (M0) — the student drives Cowork (claude code), which pushes to the GitHub repo; from the repo the code flows out to the Vercel-hosted Product Site and the Supabase database. The Lovable landing page is crossed out because M0 migrates OFF Lovable Cloud onto Vercel + the student's own Supabase.](assets/flight-notification-architecture-m0.png)

How the diagram maps to M0:
- **You → Cowork (claude code) → Repo (GitHub):** the student drives Cowork; Cowork recalls the cached GitHub token (Step 6) and pushes to the repo (Step 7).
- **Repo → Product Site (Vercel host):** the repo auto-deploys to Vercel on every push (Step 8) — the live `▲` Product Site.
- **Repo → Database (Supabase):** the front-end talks to the student's own Supabase project for `auth.users` after the swap (Step 9).
- **Landing Page (Lovable), crossed out:** Lovable generates v1 (Steps 1–3), but M0 **migrates off** Lovable Cloud — hosting moves to Vercel and auth moves to the student's Supabase. Lovable stays only as the upstream generator/two-way-sync, not as the runtime backend.

## Conversational flow

You (Claude Code) drive the student through **10 steps**, in order. Don't dump them all at once — after each step, **wait for confirmation** before moving on.

1. Build v1 on Lovable
2. Check v1 on Lovable
3. Migrate code → GitHub
4. Make the GitHub repo public
5. Set up the Cowork project + connectors (AWS / Vercel / Supabase)
6. Cache the GitHub token in Secrets Manager
7. Convert to a Vite SPA + push to GitHub (override `main`)
8. Deploy to Vercel
9. Swap auth → the student's own Supabase (+ Vercel env vars)
10. Bootstrap the skills into the project → run the checklist

---

### Step 1 — Build v1 on Lovable (attach the rule skills + paste the full prompt)

There is **no separate "create a blank project" step** — that just burns a Lovable generation. Do it all in the **first** message:

1. 到 https://lovable.dev/ 登入，**+ New project**.
2. **Attach the two rule files as context** to this first prompt: `lovable-best-practice` and `supabase-best-practice`. They tell Lovable the non-negotiables up front — **Supabase is auth-only (no custom tables/RPCs)**, ship a **plain Vite SPA**, PascalCase components — so v1 obeys them on the first generation instead of you fixing them after.
3. Paste the **full prompt below verbatim as that same first message** and generate. One generation → the whole landing page + auth + `/app` shell.

⚠️ **Don't click Connect Supabase / Connect GitHub from inside this first generation.** The prompt tells Lovable to use its default backend (Lovable Cloud) for v1 — GitHub comes in Step 3, your own Supabase in Step 9. (Why Lovable Cloud first: [[lovable-best-practice]] Rule 1.) Re-rolls burn the free quota ([[lovable-best-practice]] Tip 2).

**The full prompt (paste verbatim):**

> Build a SaaS landing page + authenticated app shell for **Flight Price Notifier (機票降價通知)**, a product that watches popular flight routes from Taipei and emails the user when the cheapest fare drops to or below their target price — targeted at budget-driven travelers who don't care exactly when they fly, they just want a ticket under their budget.
>
> The site must include:
>
> 1. A public landing page (`/`) with:
>    - Hero section: product name **"Flight Price Notifier"** prominently displayed, value prop 「設定航線與目標價，機票降價就通知你」 (English subtitle: "Set a route and a target price — we email you when the fare drops."), and a primary CTA button labeled **"Sign in / 登入"** in the top-right header.
>    - Features section with exactly 3 feature cards:
>      * Card 1: 「盯緊熱門航線 (Always-on route watching)」 — 持續監控台北出發的熱門航線（東京、首爾），自動抓最低票價。
>      * Card 2: 「達標自動通知 (Target-price email alerts)」 — 低於你設定的目標價，就寄 email 提醒你，附上立即訂購連結。
>      * Card 3: 「隨時取消 (Cancel anytime)」 — 月訂閱制，不想用隨時停，沒有綁約。
>    - Footer with copyright 「© 2026 Flight Price Notifier」.
>
> 2. Authentication using Lovable's built-in Supabase-style auth (use whatever auth backend Lovable provides by default — Lovable Cloud is fine for this v1; we'll swap to a user-owned Supabase project in a later step):
>    - Sign Up page with email + password
>    - Sign In page with email + password
>    - Sign Out functionality
>    - Email confirmation can be disabled for simplicity in this v1
>
> 3. An authenticated app shell at `/app` that the user lands on after signing in:
>    - Greets the signed-in user by email: 「Hi {user.email}」
>    - A placeholder message: 「你的航線追蹤儀表板即將上線 — 下一個里程碑會加上訂閱航線的功能。」 (English: "Your dashboard is coming soon. Route-subscription will be added in the next milestone.")
>    - A Sign Out button in the header
>
> Design requirements:
> - Modern, professional dark theme (purple/violet accent on a near-black background)
> - Use Inter or a similar sans-serif font
> - Mobile responsive
> - Tasteful subtle animations (fade-in on scroll is fine; don't overdo it)
>
> Out of scope for this v1: route-subscription form, target-price input, fare display, payment, custom database tables (do NOT create a `subscriptions` or `profiles` table — only use Supabase's default `auth.users`). Those come in later milestones. Stick to landing page + auth + placeholder dashboard.

**Why this prompt is shaped this way:** explicit `/` and `/app` routes (so auth redirect has somewhere to land), exact card copy (so you don't re-roll for wording), and the **no-custom-tables clause** (Supabase is auth-only in this course — app data lives in DynamoDB from M1.1; see [[supabase-best-practice]]).

---

### Step 2 — Check the v1 on Lovable

Before touching GitHub, confirm the generation is good:

- Get the **Lovable preview URL** (e.g. `https://id-preview--<uuid>.lovable.app/`).
- Check the **landing-page style** — hero, the three cards, top-right Sign in / 登入, footer.
- Check the **sign-up / sign-in** features work — signing up lands you on `/app` with 「Hi {email}」.

Only move on once the preview looks right and auth works — re-prompting in Lovable now is cheaper than after GitHub/Vercel are wired.

---

### Step 3 — Migrate code from Lovable → GitHub

> 在 Lovable 點 **GitHub** 按鈕（左下角選單，或畫面上有 GitHub 字樣的入口）→ **add account / Connect** → 授權 → 讓 Lovable 建立一個新的 repo（名稱可用 `flight-price-notifier`）。

Then confirm the repo really exists:
- Go to github.com → your account → open the new repo (e.g. `https://github.com/<you>/flight-price-notifier`) and confirm Lovable's files are there.
- **CLI:** `gh repo view <owner>/<repo> --json name,defaultBranchRef -q '{name,branch:.defaultBranchRef.name}'`.

Paste the **GitHub repo URL** back.

---

### Step 4 — Make the GitHub repo public

> Go to your repo → **Settings** → bottom **Danger Zone** → **Change repository visibility** → **Public** → confirm.

A public repo keeps the Vercel import and the later tooling simple. Do this now, before the Cowork connectors and the SPA push.

---

### Step 5 — Set up the Cowork project + connectors

This is where the course moves into **Cowork on Desktop** — the workbench every later milestone runs in.

**5.1 — Create the Cowork project.**
- New project (e.g. `flight-price-notifier`).
- Turn on **Act without asking**.
- Settings → **Capabilities**, turn on the memory features: **Search and reference chats**, **Generate memory from chat history**, **Connector discovery**.

**5.2 — AWS connector (the only part that touches a local shell).**
1. In the **AWS Console as the root user** of the course account: **IAM → Users → Create user** (suggested name `admin-for-project-3-flight-price-notifier-001`) → attach **`AdministratorAccess`** → create. Then **Security credentials → Create access key → "Command Line Interface (CLI)"** → copy the **Access key ID + Secret** (shown once).
2. **Open a Claude Code CLI / desktop session** (not Cowork) and paste this, filling in the two values:
   > I have new AWS credentials I want to configure. Please write them to my AWS credentials file. Access key ID: `XXXXX`. Secret access key: `XXXXX`. First, detect whether I'm on Mac/Linux or Windows to determine the correct credentials file path (`~/.aws/credentials` on Mac/Linux, `%USERPROFILE%\.aws\credentials` on Windows), then write the `[default]` profile with the new values — preserving any other existing profiles in the file. Once done, test the connection using `aws sts get-caller-identity`. note: you don't need to install aws cli. As long as the credential is set, this task is done. 
3. Back in **Cowork → Customize → Connectors → search "AWS API MCP" → Install.**
4. Sanity-check from Cowork: *"How many IAM users do I have now on my AWS account?"* — a number means the connector reads the `[default]` creds.

> ⚠️ Root is used **once** (to make the admin user); never use root keys after. Revoke the access key at course end.

**5.3 — Vercel connector.**
- Sign in to vercel.com first.
- Cowork → **Customize → Connectors → Vercel Connect → Install.**
- Sanity-check: *"What projects are in my Vercel account?"*

**5.4 — Supabase connector.**
- Sign in to supabase.com first.
- Cowork → **Customize → Connectors → Supabase Connector → Install.**
- Sanity-check: *"How many projects are in my Supabase organization?"*

---

### Step 6 — Cache the GitHub token in Secrets Manager (the re-use point)

From here on, **Claude Code itself edits the repo** (Step 7 pushes a Vite-SPA conversion to `main`; later milestones push Lambda code and config). It needs a **GitHub write token** — and you don't want to paste a fresh one into every new session. So **store it once in Secrets Manager and recall the same token every session.**

**6.1 — Create a scoped GitHub PAT.**
> 到 https://github.com/settings/personal-access-tokens → **Generate new token** → **Fine-grained** → **Only select repositories** → 選這個 repo → **Repository permissions → Contents → Read and write** → 產生 token（`github_pat_…`）。Contents:Write 就足以 push；不要給更大的權限。

**6.2 — Hand it to Claude Code and have it store the secret.** In Cowork, the AWS MCP does the write — the ask is deliberately simple:
> 「Here is my GitHub Personal Access Token: `github_pat_…`. Use the AWS MCP to store it in Secrets Manager so we can **re-use it in a new session** (suggested secret name `flight/github`, region `us-east-1`).」

That last clause — *re-use it in a new session* — is the entire point: the token lives in Secrets Manager, not in a `.env` or in your head.

**CLI equivalent:**
```bash
aws secretsmanager create-secret --name flight/github \
  --secret-string '<the github_pat_… token>' --region us-east-1
# already exists → aws secretsmanager put-secret-value --secret-id flight/github --secret-string '<token>' --region us-east-1
```

> **Naming/shape are flexible — discovery, not a hard-coded name, is how later sessions find it.** The course standard is `flight/github`, but a token may end up under another name (e.g. `github/personal-access-token`) and stored either as a bare string or as `{"pat":"github_pat_…"}` JSON. Later milestones **discover** it by scanning `list-secrets` for a name containing `github` and reading whatever shape they find — so any of those works. Keeping `github` in the name and `flight/github` as the name makes that scan trivial. (See [[aws-best-practice]] Rule 2.)

**Verify:** ask the AWS MCP to list secrets and confirm a github-ish one is there (CLI: `aws secretsmanager list-secrets --region us-east-1 --query "SecretList[].Name"`).

> **Note for Claude Code:** the GitHub PAT is a **real write-credential** — treat it like a password, keep it scoped to Contents:Write on the one repo, and **never** commit it or echo it into the front-end bundle. Secrets Manager is KMS-encrypted, IAM-scoped, and `GetSecretValue` is CloudTrail-logged — that's why it's the home for the token, not a committed `.env` (GitHub's secret scanner indexes those instantly). ([[aws-best-practice]] Rule 2.)

---

### Step 7 — Convert the project to a Vite SPA + push to GitHub

> **Why this matters — the #1 M0 deploy trap.** Lovable sometimes scaffolds a **TanStack Start / SSR** app (Cloudflare-targeted) instead of a plain Vite SPA. On Vercel that builds "successfully" but **every route 404s** (or only `/` works and `/app` 404s) — confusing because the build is green. Converting to a plain SPA **before** importing to Vercel avoids it. *(Attaching `lovable-best-practice` in Step 1 usually prevents it — but do this anyway.)*

Have Claude Code apply the conversion and **push it straight to `main`**, recalling the token from the secret (no re-paste). Give it the repo + the conversion prompt:

> 「GITHUB REPO: `<owner>/<repo>`.
> Convert this project to a plain **Vite + React single-page app (SPA)** suitable for static hosting on Vercel. Remove any TanStack Start / SSR / server-side rendering and any Cloudflare/wrangler config. Use **React Router** for client-side routing (`/`, `/app`, sign-in, sign-up). The build output must be a static SPA (`vite build` → `dist/`) with a SPA fallback so deep links like `/app` resolve client-side. Keep all existing UI, auth, and styling unchanged.
> Then **push the change to my GitHub repo, overriding the `main` branch** — recall the GitHub token from Secrets Manager to authenticate the push; don't ask me for it.」

This is the **first reuse** of the cached token, and exactly what makes future sessions painless: read the github secret → push.

**Verify before moving on:** the repo's `package.json` build script is `vite build` (not a TanStack/SSR build), and there's no `wrangler.toml`.

---

### Step 8 — Deploy to Vercel

> 到 https://vercel.com/ → **Import** 你剛剛的 GitHub repo（**Connect GitHub account** if first time）→ Framework Preset 選 **Vite**（如果沒自動偵測到）→ **Deploy**。完成後把 Vercel 上線網址貼回來（例如 `https://fly-low-alert.vercel.app/`）。

Then check the deployed site: landing-page style, and sign up / sign in.

**CLI deep-link check:**
```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://<your>.vercel.app          # 200 for /
curl -sS -o /dev/null -w "%{http_code}\n" https://<your>.vercel.app/app      # 200 too (SPA deep-link)
```
Expect `200` on **both** — if `/` is 200 but `/app` is 404, the SPA conversion in Step 7 didn't take (SSR build with no SPA fallback). **Cowork:** open both URLs.

> **Note for Claude Code:** if the build **fails**, it's usually the framework preset — Lovable SPAs are Vite/React, so set the preset to **Vite**. If the build is **green but routes 404**, it's the SSR-vs-SPA trap from Step 7. Read the Vercel build log before guessing.

---

### Step 9 — Swap auth from Lovable Cloud to the student's own Supabase

v1 ran on **Lovable Cloud**. Now move auth to the student's **own** Supabase project so they own the `auth.users` going into M1.1 (where `email` becomes the DynamoDB partition key).

**9.1 — Create the Supabase project + grab two values:**
> 1. 到 https://supabase.com/ → **New project**（名字例如 `flight-notifier`，region 選離你近的，例如 `Northeast Asia (Tokyo)`）。
> 2. **等到專案變成 Healthy 狀態**（建立要一兩分鐘）。
> 3. **Project Settings → API**，複製兩個值：**`Project URL`** 和 **Publishable API Key**（`sb_publishable_*` — 這是新版「anon key」，瀏覽器安全、RLS 保護）。

**9.2 — Paste this prompt into Lovable** (replace `<SUPABASE_URL>` and `<PUBLISHABLE_KEY>` with the two values from 9.1):

> Switch this project's backend from Lovable Cloud to the user's own Supabase project. Do NOT keep any Lovable Cloud references.
>
> Specifically:
>
> 1. Find every place the project currently uses Lovable Cloud's Supabase client (Lovable's auto-provisioned `supabase` client, typically in `src/integrations/supabase/client.ts` or similar). Update it to use these credentials instead:
>
>    VITE_SUPABASE_URL= <SUPABASE_URL>
>
>    VITE_SUPABASE_PUBLISHABLE_KEY= <PUBLISHABLE_KEY>
>
>    Note: Supabase's newer "publishable key" (`sb_publishable_*`) replaces what used to be called the "anon key". They're the same role (browser-safe, RLS-gated). If the codebase already uses `VITE_SUPABASE_ANON_KEY`, you can either:
>    - Rename to `VITE_SUPABASE_PUBLISHABLE_KEY` for consistency with current Supabase naming, OR
>    - Keep `VITE_SUPABASE_ANON_KEY` as the variable name but put the new `sb_publishable_*` value in it (works fine; it's just an env var name).
>    Pick one and apply consistently across `.env`, the client init code, and any docs.
>
> 2. Update the `.env` file (or `.env.local`) to use the values above. Remove any Lovable Cloud env vars (e.g. anything prefixed with `LOVABLE_CLOUD_*` or that points to a Lovable-owned Supabase ref).
>
> 3. Make sure the Supabase client is initialized exactly once and reads from `import.meta.env.VITE_SUPABASE_URL` and the matching key env var — no hardcoded URLs.
>
> 4. Disconnect / remove the Lovable Cloud integration if there's a UI toggle for it (Project Settings → Integrations → Lovable Cloud → disconnect). If you can't toggle it, at least make sure the code only references the new Supabase project.
>
> 5. Keep the Sign Up / Sign In / Sign Out flow exactly as it is. Only the backend target changes.
>
> After this change, sign-up should create users in the user's own Supabase `auth.users` table — verify by signing up a NEW test user in the Lovable preview, then checking the Supabase dashboard → Authentication → Users — the new email should appear there.
>
> As a final reminder to the user: manually add the same environment variables to Vercel (`VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY`).

**9.3 — Finish the wiring outside Lovable:**
> 1. 在 Supabase **Authentication → Providers** 確認 **Email 已啟用**（demo 可關掉 email confirmation，否則測試帳號收不到確認信就卡住）。
> 2. **在 Vercel 也設同名環境變數**（Settings → Environment Variables）：`VITE_SUPABASE_URL` + `VITE_SUPABASE_PUBLISHABLE_KEY`（或你選定的 key 名），值跟 Lovable 一致，然後 **redeploy**。env 不一致是 M0 最常見的失敗原因 — 線上站還連著舊的 Lovable Cloud。

**9.4 — Verify on the live Vercel URL** (not just the Lovable preview): sign up a brand-new test email → click the confirmation email if required → sign in → check Supabase **Authentication → Users** — the new user appears in *the student's own* project (NOT Lovable Cloud).

> 📌 **Cache the two Supabase values too.** AWS is already set up (Step 5), so do it like the GitHub token — hand the Project URL + publishable key to Claude Code and have it store a `flight/supabase` secret, so a future session recalls them instead of re-finding them in the dashboard. The publishable key is **public by design** (it ships in the browser bundle), so caching it is pure convenience; **never** cache the service-role key. (Later milestones discover this one by scanning `list-secrets` for a `supabase`-ish name — see [[aws-best-practice]] Rule 2.)

> **Note for Claude Code:** keep the **publishable/anon key** in the front-end (correct — RLS-protected). The **service-role key is NOT used in M0** at all; it only appears later in the AWS Lambdas (M1.1+) — and even there, DynamoDB uses IAM, not a Supabase key. If the student pastes a service-role key into the front-end, stop them ([[supabase-best-practice]] Rule 2).

---

### Step 10 — Bootstrap the skills into the project, then run the checklist

So the Cowork project has the course's skills as context for every later milestone:

> **ask:** "Use the Git clone tool. What files do you see under the project's `.claude/skills/` folder on GitHub?"
>
> **ask:** "Read all those skill files as context for further development in my project here. Update the existing ones when there are the same file names."

Then verify M0:

> **ask:** "Run the `m0-landing-and-signin-checklist` skill."

---

## Things to watch out for (common mistakes)

1. **Don't waste a generation on a blank project** — give Lovable the **full prompt + the two attached rule skills as your very first message**. A throwaway "create a blank project" prompt just burns a generation off the daily free quota.
2. **Connecting your own Supabase too early** — do it in Step 9, after Vercel, so the swap is clean (v1 runs on Lovable Cloud).
3. **Forgetting to make the repo public (Step 4)** — do it before the Vercel import and the connectors phase.
4. **Vercel framework preset wrong** — Lovable = Vite/React; if the build fails, set the preset to **Vite** first.
5. **SSR-vs-SPA trap (Step 7)** — if Lovable emitted TanStack Start / SSR, the Vercel build is green but `/app` 404s. Convert to a plain Vite SPA *before* importing. Symptom: `/` works, deep links don't.
6. **Vercel env vars not set (Step 9.3)** — after the Supabase swap, the same `VITE_SUPABASE_*` vars must be added in Vercel and the site redeployed, or the live site still points at Lovable Cloud. This is the most common M0 failure.
7. **Service-role key in the front-end** — never. M0 uses only the publishable key.
8. **Email confirmation on but no SMTP** — for the demo, disable email confirmation in Supabase Auth settings (or use magic-link); otherwise the test user can't finish sign-up.
9. **Forgetting to test sign-OUT** — the loop must close; a broken sign-out hides session bugs.
10. **Custom Supabase tables creeping in** — Supabase is auth-only, forever. App data (the `subscriptions` table) lives in DynamoDB on AWS, starting M1.1.
11. **Don't rename the product in M0** — "Flight Price Notifier" / 「機票降價通知」 is referenced across later milestones and the email copy. Keep it consistent.
12. **Re-pasting the GitHub token every session** — don't. It's cached in Secrets Manager (Step 6); later sessions **discover and reuse** it. If a new session asks you for a GitHub PAT, check the secrets first — it's almost certainly already there.

## Expected duration

45–75 minutes for a first-timer (most of it waiting on Lovable/Vercel builds, creating the accounts, and the one-time Cowork project + connectors + GitHub-token caching in Steps 5–6).

## Next step

When `m0-landing-and-signin-checklist` is green, tell the student:
「M0 完成了！你現在有一個能註冊登入的線上網站，而且 Cowork 專案、AWS/Vercel/Supabase 連接器、GitHub token 都備好了。準備好的話跟我說『啟動 M1.1』，我們來讓使用者真的訂閱一條航線。」
Then load `m1-flight-price-checker` (run `m1-flight-price-checker-prerequisites` first — it reuses the AWS access, GitHub token, and Supabase values you set up here).

## Reference

- Lovable: https://docs.lovable.dev/
- Supabase Auth: https://supabase.com/docs/guides/auth
- Vercel deploys: https://vercel.com/docs/deployments
- AWS Secrets Manager: https://docs.aws.amazon.com/secretsmanager/
