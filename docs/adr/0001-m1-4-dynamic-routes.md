# ADR 0001 — M1.4 user-defined routes

- **Status:** Accepted for Phase 0 (design only). Phases 1–5 each need separate approval.
- **Date:** 2026-11-04; revised 2026-11-05 (D3 worldwide catalog, D4 total limit, D12–D14);
  amended 2026-11-12 (D1: several departure months per route, R-MM-1, owner-approved)
- **Scope:** subscriptions API, fare check (wrapper/parser), front end, deployment.
  The notification Lambda, `notification_history`, deduplication, the SQS trigger and the
  EventBridge schedule are **out of scope and frozen**.

## Context

M1 supports two fixed plans (TPE→TYO, TPE→SEL). M1.4 lets a signed-in user create, edit,
pause, resume and delete subscriptions for any supported origin/destination pair with a TWD
target. Constraints: keep Supabase for auth and DynamoDB for data, preserve existing
subscriptions and notification history, minimise Travelpayouts requests, and change nothing in
the notification path.

## Decisions

### D1. Keep the `subscriptions` table; one subscription per route **and departure month**
*Amended 2026-11-12 (R-MM-1, owner-approved). The original text allowed one subscription per
route pair, whatever the month; that is superseded.*

The table and its key schema stay: `email` (partition) + `route` (sort). The **value** of the
sort key identifies one subscription:

- legacy rolling row (M1, no `departure_month`): `ORIG-DEST`, e.g. `KHH-TYO` — unchanged;
- fixed-month row: `ORIG-DEST#YYYY-MM`, e.g. `KHH-TYO#2027-01`.

Rules:
- A user may hold several fixed months of one route (`KHH-TYO` 2027-01, 02 and 03 are three
  subscriptions).
- At most one subscription per user, route and fixed month, and at most one rolling row per
  user and route. A rolling row may coexist with fixed months of its route, including the
  month it currently resolves to (overlap policy below).
- Every row counts toward the limit of 10 (D4), paused and expired included.

The API's public, URL-safe subscription ID is the sort key with `#` replaced by `_`
(`KHH-TYO_2027-01`). It is used in `PATCH`/`DELETE /subscriptions/{id}`. Authorisation is by
the session's email (the partition), never by the ID. A route or month change moves the row to
its new key in one transaction (D5). Mapping, rationale and tests:
[m1.4-r-mm-1-multi-month.md](../m1.4-r-mm-1-multi-month.md).

**Migration and compatibility.** No data migration: existing M1 rows keep their keys and stay
rolling. New attributes are optional and absent values have safe defaults.

New attributes:
- `alerts_paused` (bool, absent = not paused);
- `departure_month` (`YYYY-MM`, absent = legacy rolling next-month row, D12; on fixed rows it
  always equals the key's month);
- `origin` and `destination`;
- `plan_name` becomes optional (kept on the two M1 rolling rows only);
- `previous_route` (the previous sort key, set when a route or month is changed, for audit).

`route_type` and display names are derived from the allowlist.

**Rolling/fixed overlap.** The notification dedup stream stays (user, route pair, effective
month) (Phase 2B-1). Subscriptions resolving to the same stream share it:
- The fare check sends one message per user and stream, listing every matched subscription.
- The notification Lambda sends if **any** listed subscription is still eligible. A paused,
  expired or deleted one never suppresses another.
- The M1 re-alert rule applies within the stream (no second email for the same fare).

Full policy:
[m1.4-r-mm-1-multi-month.md §4](../m1.4-r-mm-1-multi-month.md#4-rollingfixed-overlap-policy).

### D2. Pause is `alerts_paused`, never `subscription_status`
`subscription_status` is reserved for M2's payment paywall. "Paused by the user" and "not paid"
are independent; from M2 an alert requires `not alerts_paused AND subscription_status = active`.

### D3. Curated, file-driven allowlist (worldwide, 95 destinations)
`aws/config/airports.json` (schema_version 2, 2026-11-05): origins TPE, KHH, RMQ; 95
destinations in seven regions — Asia (31, the original 25 kept first and unchanged), Middle
East (3), Europe (27), North America (17), Central and South America (6), Australia (7) and
New Zealand (4). Each entry has `code`, `type`, `name_zh`, `name_en`, `country` and (for
destinations) `region`; the file also carries `regions` (display order and names) and
`countries` (names, for search).

- `type: "city"` is an IATA **metropolitan** code that searches several airports, listed in
  `airports` (LON = LHR, LGW, STN, LTN, LCY, SEN; NYC = JFK, EWR, LGA). `type: "airport"` is
  one airport (FRA, AKL) and has no `airports` field. An airport covered by a city entry is never
  also its own entry (no HND next to TYO), so the same flights are never offered twice.
- Every code was checked on 2026-11-05 against OpenTravelData's `optd_por_public.csv` (current
  records only: code exists in the stated country, airports are airports, city codes are city
  codes and each listed airport belongs to that city) with `aws/tools/verify_airports.py`,
  which future expansions must re-run.
- The origin `TPE` is the Taipei city code (`airports: ["TPE", "TSA"]`, Taoyuan + Songshan),
  as M1 already uses it. Because `TPE` is also Taoyuan's airport code, Phase 2 must confirm in
  `DRY_RUN` that Travelpayouts applies city semantics (plan: "Origin TPE", checks T1–T5).
- The API validates against the file server-side; the front end reads the same file at build
  time. Expanding = editing the file and re-running the check (no schema change).
- **The catalog is only an allowlist.** It never drives fare queries: the fare check looks up
  only `(origin, destination, effective_month)` groups that have at least one active,
  non-expired subscription (D12), so adding destinations adds no Travelpayouts requests.

### D4. Strict total limit of 10 subscription records per user
Owner decision 2026-11-05: `aws/config/subscription-policy.json` →
`max_subscriptions_per_user = 10`, passed to the API as `MAX_SUBSCRIPTIONS`. **Every record
counts** — active, paused and expired, including existing M1 rows. Pause doesn't release a slot,
resume doesn't consume one, expiry doesn't release one; only delete releases one. The UI shows
「我的航線訂閱：X / 10」.

Enforcement is strict under concurrency: a per-user record in a separate `subscription_quota`
table (`count`, `version`) is updated, with a `version` condition, in the same transaction as
every create and delete. The handler reads the quota version **first** and the strongly
consistent row count **second**, retries a failed version condition at most 3 times with bounded
jittered backoff, then returns `409 conflict`. Users with no quota record (all M1 users) get one
on their first create/delete via `attribute_not_exists`. Edits, month changes, pause/resume and
route changes don't change the count and don't touch the quota. The legacy `POST /subscribe`
is served by the same code path after cutover (D14). Details and the concurrency matrix:
`docs/api/m1.4-subscriptions-api.md` §5; executable model and interleaving tests:
`aws/reference/subscriptions_quota.py`, `aws/tests/test_m14_quota_model.py`.

### D5. Route changes are a transaction
Changing origin/destination changes the sort key, so it is one `TransactWriteItems`:
`Delete(old, condition attribute_exists)` + `Put(new, condition attribute_not_exists)`.
Duplicates are impossible (the `Put` condition fails → `409 duplicate_subscription`);
a vanished source row → `404`; a stale client (`if_updated_at` mismatch) → `409 conflict`.
Creating uses a `Put` with `attribute_not_exists` (plus the quota update, D4) for the same
reason. Target, month and pause edits are single `UpdateItem`s with `attribute_exists`.

### D6. Notification history is never changed by the subscriptions API
History is keyed `{email}#{route}`; the API's IAM role has **no access** to that table.

| User action | Effect on alerts |
|---|---|
| Change target price | Same key; the 24 h floor / re-alert rules still apply to the last alert |
| Change departure month | Same key; within 24 h of the last alert the new month alerts only if ≥ 20 % / ≥ NT$2,000 cheaper than the last alerted price, else after the 24 h floor (≤ 24 h delay) |
| Expire / reactivate | Expired rows are skipped; reactivating keeps the key, so normal dedup applies |
| Pause → resume | Same key; dedup continues from the last alert (no instant repeat) |
| Delete | History kept |
| Delete then re-create the same route | Same key, so dedup applies (stops delete/re-create alert spam) |
| Change route A → B | B is a new key and may alert immediately; A's history is kept |
| Change B back to A within 24 h | A's history applies |

Purging history (e.g. account deletion) is a separate, future, approved process.

### D7. TWD is the fare currency; the USD lookup is under evaluation
Gating, dedup and the headline stay TWD. The USD lookup only feeds the optional 「約 US$」 line
and doubles Travelpayouts calls. Options: drop it (email becomes TWD-only with no template code
change, since the line is already optional), or derive USD from one currency-rate request per
run. Decided after the M1 email test; M1 behaviour is unchanged until then.

### D8. Travelpayouts v3 migration is gated and side-by-side
`/v1/prices/cheap` is documented as outdated; the replacement is
`/aviasales/v3/prices_for_dates`. v3 defaults to **one-way** (`one_way=true`), so the parser
must send `one_way=false` explicitly to keep today's round-trip semantics, and must validate
that each accepted offer has a `return_at` and a `departure_at` inside the requested
(effective) month. Neither endpoint is
called with `market` today (default `ru`); that stays unchanged during migration. The switch
is behind a `FARE_API=v1|v3` setting (default v1) and happens only after a dry-run comparison
passes (see the plan, Phase 2).

### D9. Least-privilege roles
A new `flight-subscriptions-api-role` (DynamoDB `subscriptions` and `subscription_quota` only;
no SSM, KMS, SQS, Lambda or `notification_history`). The Travelpayouts and Resend keys stay readable only by
the fare-check and notification functions. Design files: `aws/iam/`.

### D10. Deployment via GitHub Actions + AWS OIDC with manual approval
No long-lived AWS keys in GitHub. A plan role (PRs/main) can create change sets but is
explicitly denied execution; a deploy role is assumable only from the `production` GitHub
environment, which requires the owner's approval; CloudFormation runs as `flight-cfn-exec`,
which cannot touch IAM, SSM, table deletion or queue purges. Lambda code ships as S3
artifacts, removing the 4,096-byte inline limit. Design: `docs/deploy/github-actions-oidc.md`.

### D11. Point-in-time recovery before any new write path
Enable PITR on `subscriptions` (and `notification_history`) before Phase 1 writes.
Cost: about US$0.20 per GB-month of table size (AWS's example rate) plus about US$0.15 per GB
restored; both tables are under 1 KB, so effectively US$0.00/month. The retention period
doesn't change the price.

### D12. Departure month per subscription
Owner decision 2026-11-05. The user picks one departure month from the **next 12 calendar
months starting next month** (Asia/Taipei); the default is next month; it is stored as
`departure_month = "YYYY-MM"` and can be edited. A fixed month that has passed makes the row
**expired** (derived at read/check time, never written by a job): no fare checks, no
notifications, shown as 「已過期」, still counted (D4); choosing a new month in the window
reactivates it. Existing M1 rows have no month and stay **rolling** (checked for next month,
as today); they are never converted automatically, and become fixed only when the user picks a
month. The API reports `month_mode` (`fixed`/`rolling`) and `effective_month`. The fare check
groups work by `(origin, destination, effective_month)`, so each month gets its own result and
one Travelpayouts lookup serves every subscriber of that group (plan, Phase 2).

### D13. Prototype route is fail-closed
`/prototype/subscriptions` is compiled in only for the local dev server (`vite`, `vercel dev`)
and Vercel **preview** builds (`VERCEL_ENV === "preview"`, exact match). Production is excluded
whatever any variable says; unset or unknown environments (a local `vite build`, other hosts,
custom Vercel environments) are excluded. The decision is made at build time by
`src/lib/prototype-flag.ts` and injected as a constant, so a production bundle contains neither
the route nor the page.

### D14. Deployment order: nothing user-reachable before the fare check supports it
The deployed parser checks next month for every row and ignores pause and month. Therefore the
new API's routes are not publicly reachable until the wrapper/parser support dynamic routes,
fixed months, paused and expired rows, the quota, and the unchanged notification message
format. At cutover one reviewed change set points `POST /subscribe`, `GET /subscriptions` and
the new routes at the new handler together, so no quota-unaware writer runs alongside it.
Phase 1B (API stack) and Phase 2 (fare check) may ship as separate change sets only if the new
routes stay unattached/inaccessible in between; otherwise they ship together.

### D15. Multi-platform search links are presentation, separate from fare detection
(2026-11-06, proposed) Fares still come only from the Travelpayouts Data API. Search links to
Skyscanner Taiwan, Trip.com Taiwan, Aviasales and (fallback) Google Flights are built by a pure
module from route and dates at render time, in the notification email (Phase 2B) and in the
browser (Phase 3), driven by one configuration file. They are never computed by the wrapper or
parser, never stored, and never added to the SQS message, so link formats, platform order and
future affiliate parameters change without touching fare detection, deduplication or any table
schema. A detected fare is always presented as a reference price; no link may become one-way.
Details: [Multi-Platform Flight Search Links](../m1.4-multi-platform-search-links.md).

## Alternatives considered

- **New table / composite key with a subscription ID** (allow several targets per route):
  more flexible but needs a migration and changes the dedup key. Rejected for M1.4.
- **Counter item inside the `subscriptions` table**: exact, but the deployed M1.1 list Lambda
  and the parser read every row and would break on a non-subscription row. Rejected in favour of
  the separate `subscription_quota` table.
- **Active-only limit** (paused rows free a slot): rejected by the owner; a total limit is simpler
  to explain and keeps resume from ever failing.
- **Departure month in the sort key** (several months per route): originally rejected
  ("changes the dedup key and needs a migration"). **Adopted 2026-11-12 (R-MM-1)** in a form
  that needs neither:
  - legacy rows keep their keys, so there is no migration;
  - the dedup stream already includes the month (Phase 2B-1), so the dedup key is unchanged.
- **Opaque subscription UUIDs** for R-MM-1: rejected. They would need a stored attribute and a
  lookup on every request, and DynamoDB already enforces uniqueness on the deterministic key.
- **Free-text airport search (Travelpayouts autocomplete)**: better UX, but unpopular routes
  often have no cached fares, and every keystroke would be an external request. Instead the UI
  searches the curated catalog locally (Chinese name, English name, IATA code, country or
  region). Autocomplete deferred until the allowlist proves out.
- **Every airport in a metro area as separate entries**: more precise, but duplicates fares and
  confuses users (Tokyo vs Narita vs Haneda). Rejected in favour of city codes where they exist.
- **Store search URLs in the SQS message or a table**: rejected (D15); formats change and would
  need data migrations or parser deployments.
- **Skyscanner referral endpoint without a partner ID**: rejected for v1; its redirect attaches
  tracking parameters not issued to this project (search links spec §3.2).
- **Keep inline CloudFormation code**: no new infrastructure, but the notification code is
  already at the limit and reading code back through the AWS connector is unreliable for
  non-ASCII text. Rejected for the long term.

## Consequences

- No data migration; the two existing subscriptions stay rolling and keep alerting as today, and
  all history keeps working unchanged.
- A month change can delay the first alert for the new month by up to 24 h (D6); accepted to
  keep the notification Lambda unchanged.
- The notification Lambda needs no change for new routes (it shows unknown city codes as the
  bare code until `CITIES` is extended, a settings-only change after M1 closes).
- One-time IAM bootstrap (OIDC provider, three roles, artifacts bucket) needs owner approval.
