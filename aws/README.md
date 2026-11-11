# AWS back end (M1)

Region **ap-southeast-2**, account resources shared by all M1 Lambdas via the existing
`flight-lambda-role`. Secrets live in **SSM Parameter Store** (SecureString), never in code or logs:
`/flight/travelpayouts`, `/flight/resend`, `/github/pat`.

## Part 1.1 — subscribe API

Stack `flight-m1-subscribe-api` (HTTP API `flight-api`: `POST /subscribe`, `GET /subscriptions`)
with Lambdas `flight-save-subscription` and `flight-list-subscriptions`. Their source is in
`aws/lambdas/` and `aws/stacks/build_m1_subscribe_api.py` reproduces the deployed template
byte-for-byte. The Supabase URL and publishable key are build inputs
(`SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`), not committed values. Both Lambdas identify the
user from the Supabase session token; direct IAM invokes may pass `test_email` for testing.

## Part 1.2 — fetch prices on a schedule

| Resource | Name | Managed by |
|---|---|---|
| S3 bucket (private, SSE-S3) | `flight-config-<account>` holding `flight-routes.json` | created directly |
| SQS queue (SSE-SQS) | `flight-fare-queue` | created directly (Part 1.3 sets its visibility timeout) |
| Role policy statements | `ReadRoutesConfig`, `EnqueueFareMatches`, `FanOutToParser` in `flight-data` | merged into the existing inline policy |
| Lambdas | `flight-parser`, `flight-parser-wrapper` | CloudFormation stack `flight-m1-fetch-prices` |
| EventBridge rule | `flight-price-check`, `rate(30 minutes)` → wrapper; state = stack parameter `ScheduleState` (default `DISABLED`) | same stack |
| Log groups | `/aws/lambda/flight-parser*`, 30-day retention | same stack |

Flow: EventBridge → `flight-parser-wrapper` reads `flight-routes.json` → async-invokes
`flight-parser` per route → parser fetches next month's cheapest fare from Travelpayouts
(TWD gates, USD best-effort; token sent as the `X-Access-Token` header) → scans `subscriptions`
for the route → sends one SQS message per subscriber whose `target_price` ≥ the TWD fare.
M1 has no payment guard.

### Change and redeploy

1. Edit `aws/lambdas/<fn>/index.py` (each must stay ≤ 4096 chars: the code is inlined).
2. `python3 -m unittest discover -s aws/tests -v`
3. `python3 aws/stacks/build_m1_fetch_prices.py <account-id> > template.json`
4. Update the stack `flight-m1-fetch-prices` with that template. The schedule stays **DISABLED** unless
   you pass `ScheduleState=ENABLED`; to keep it running across an update, pass `ENABLED` explicitly.

Routes config: edit `aws/config/flight-routes.json` and upload it to the bucket as `flight-routes.json`.

## Part 1.3 — email on target

| Resource | Name | Managed by |
|---|---|---|
| Lambda | `flight-fare-notification` (+ 30-day log group) | CloudFormation stack `flight-m1-email-on-target` |
| SQS trigger | `flight-fare-queue` → notification, batch 5, partial-batch failures | same stack |
| Queue setting | `VisibilityTimeout=180` (> 30 s Lambda timeout) | set directly |
| Role policy statement | `ConsumeFareQueue` (ReceiveMessage, DeleteMessage, GetQueueAttributes) | merged into `flight-data` |

Per message: query `notification_history` (`pk = "{email}#{route}"`, newest first) → send only if
never alerted, the last alert is older than `NOTIFY_FLOOR_HOURS` (24), or the fare dropped
≥ `REALERT_PCT` (20 %) or ≥ `REALERT_ABS_TWD` (NT$2,000) since → POST to Resend (key + `from` from
SSM `/flight/resend`) → write the history row (`price` in TWD) only after a 2xx.
`429`/`5xx` → that message is retried; `403`/`422` → logged (addresses masked) and dropped.
Logs identify subscribers by a short hash, never by address.

Email content: NT$ headline (+ 約 US$ when available), target price, travel dates, and
`航空公司：<name>（<code>）` using `aws/config/names.json` (passed as the `CITIES` / `AIRLINES`
env vars; an unknown code is shown as the bare code). The 「立即訂購」 link is an Aviasales search:
`https://www.aviasales.com/search/<ORIGIN><DDMM><DEST><DDMM>1` (trailing `1` = one adult, which
Aviasales requires). The notification code sits at the 4,096-byte inline limit: add display names
to `names.json`, not to the code.

Planned (Phase 2B, not implemented): the single 「立即訂購」 link becomes a reference-fare line
plus search links to Skyscanner Taiwan, Trip.com Taiwan, Aviasales and Google Flights, with a
price disclaimer — see [Multi-Platform Flight Search Links](../docs/m1.4-multi-platform-search-links.md).
The message format the Lambda reads does not change.

Phase 2B-1 (code only, not deployed): `aws/lambdas/flight-fare-notification-v2/` replaces this
Lambda's deduplication with month-aware streams (subscriber, route, departure month), atomic
claim/commit and a Resend idempotency key, keeping the M1 rule within each stream and the M1
email unchanged. Design, deployment steps and rollback: `docs/m1.4-phase2b1-month-dedup.md`.

### Affiliate marker (not enabled on this M1 Lambda)

M1 links carry no marker. Phase 2B-2 (code only, not deployed) adds it to the v2 notification
Lambda: the Travelpayouts partner ID ("marker", not a secret but an account identifier, so never
committed) is read from the env var `TP_MARKER` at deploy time and appended as `?marker=<id>` to
Aviasales links only. Without a valid marker, v2 shows only the plain Aviasales link. The
Travelpayouts API token is unrelated and must never appear in links. See
`docs/m1.4-phase2b2-search-links.md`.

While `from` is `onboarding@resend.dev`, Resend only delivers to the Resend account's own
address (others get `403`; `example.com` addresses get `422`). Verify a domain (M3) to email anyone else.

Redeploy: `python3 aws/stacks/build_m1_email_on_target.py <account-id> > template.json`, then
update stack `flight-m1-email-on-target`. After changing `/flight/resend`, change any env var on
the function to force a cold start (the parameter is cached per container).

## M1.4 (planned)

- `aws/config/airports.json`: curated origin/destination allowlist (schema_version 2: 95
  destinations in seven regions, city vs airport codes). Re-verify after editing:
  `python3 aws/tools/verify_airports.py optd_por_public.csv` (OpenTravelData reference file).
- `aws/config/subscription-policy.json`: 10 total subscription records per user, TWD target
  range, 12-month departure window.
- `aws/reference/subscriptions_quota.py`: executable model of the strict quota protocol.
- `aws/lambdas/flight-subscriptions-api/`: the M1.4 API (Phase 1B, **not deployed**). Package
  with `python3 aws/scripts/package_subscriptions_api.py`; template
  `aws/stacks/build_m14_subscriptions_api.py` (no API routes); change-set preparation
  `aws/scripts/plan_m14_subscriptions_api.sh` (dry run by default, never executes).
  Integration tests: `pip install -r aws/requirements-test.txt`, then the usual unittest command
  (local server), or `aws/scripts/test_dynamodb_local.sh` (DynamoDB Local in Docker).
- `aws/integration/`: B4 concurrency validation — shared scenarios (`m14_scenarios.py`), the
  temporary runner Lambda, its packager and the isolated test-stack template
  (`build_test_env.py`). Plan: `docs/m1.4-b4-dynamodb-validation-plan.md`. Nothing here is
  deployed without owner approval.
- `aws/lambdas/flight-fare-check/`: Phase 2A dynamic fare check (**not deployed**; the M1
  `flight-parser*/index.py` files are unchanged and still what runs). See "M1.4 Phase 2A fare
  check" below.
- `aws/iam/`: least-privilege IAM **design** documents for GitHub Actions + OIDC deployment and
  the subscriptions API role. Not deployed.
- Plan: `docs/m1.4-plan.md`; decisions: `docs/adr/0001-m1-4-dynamic-routes.md`;
  deployment design: `docs/deploy/github-actions-oidc.md`.

## M1.4 Phase 2A fare check (not deployed)

One package, two handlers, one deterministic ZIP (`python3 aws/scripts/package_fare_check.py`):

| Handler | Replaces (after approval) | Does |
|---|---|---|
| `wrapper.handler` | `flight-parser-wrapper` | Scans `subscriptions` **without `email`** (route, origin, destination, departure_month, alerts_paused, target_price) once per run, fixes the run's single `as_of` instant, drops paused/expired/unknown rows and builds one work item per distinct `(origin, destination, effective_month)`. **Live:** invokes the parser per work item with `month` and `as_of`. **Dry run:** evaluates every work item itself, in-process, and returns a report; it invokes nothing |
| `parser.handler` | `flight-parser` | Live work item → Travelpayouts (v1 or v3) → validated cheapest round-trip TWD fare → rows of that route **and month** (resolved at the payload's `as_of`), active, target ≥ fare → SQS (M1 message shape, key for key) |

Package layout (`farecheck/`): `workitems.py` (rows → work items, rotation), `months.py` (same
rules as the API; parity-tested), `travelpayouts.py` (v1/v3 adapters and offer validation),
`matching.py`, `status.py` (fare-check records, API contract §3.4), `compat.py` (v1/v3
comparison for B5), `effects.py` (the only place with side effects), `runner.py`.

### Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `FARECHECK_MODE` | **none — required** | `dry_run` or `live`. Missing, empty or any other value: `ConfigError` before any AWS client exists (fail-closed) |
| `DRY_RUN` | — | Optional consistency check only: if set, it must agree with `FARECHECK_MODE` (`1/true` with `dry_run`, `0/false` with `live`); a disagreement refuses to run |
| `FARE_API` | `v1` | `v1` = M1's `/v1/prices/cheap` query; `v3` = `/aviasales/v3/prices_for_dates` with `one_way=false`, `currency=twd`, `sorting=price`, `limit=V3_LIMIT`, no `market` |
| `FETCH_USD` | on | M1's best-effort USD lookup for the 約 US$ line; `0` halves Travelpayouts calls |
| `V3_LIMIT` | `30` | Offers per v3 request (documented default 30, max 1000) |
| `MAX_WORK_ITEMS` | `500` | Live work items per run (rotating window, below) |
| `DRY_RUN_MAX_ITEMS` | `50` | In-process dry-run lookups per run (rotating window); keeps a dry run inside the wrapper's timeout |
| `SCHEDULE_PERIOD_MINUTES` | `30` | Must equal the EventBridge rate; drives the rotation |
| `FARE_QUEUE_URL`, `TP_PARAM`, `PARSER_FUNCTION` | as M1 | Unchanged |

Travelpayouts limits and guidance (written reply, 2026-11-07): v3 `prices_for_dates` allows up
to 600 requests per minute; refreshing a local cache about every 24 hours is recommended; TWD
amounts are conversions and must be shown as approximate (「約 NT$」) once Phase 2B/3 display
them. The fare check only produces numbers; labels are added where fares are displayed (the M1
email's 「NT$」 line changes in Phase 2B, search links spec §8.5).

### Dry run is fail-closed

- **No downstream call exists in dry run.** The wrapper evaluates work items in-process
  (`runner.DryRunEvaluator`); it never builds a Lambda invoker, so a dry run cannot reach the
  production parser — including the deployed **M1 parser**, which doesn't understand any
  dry-run flag. `LambdaInvoker` refuses to be constructed in dry run, and `run_wrapper` refuses
  a dry run that has an invoker or lacks the evaluator.
- **Clients are allow-listed.** Every boto3 client comes from `effects.guarded_factory`. Dry
  run allows only `ssm` (the Travelpayouts token): a `lambda`, `sqs` or `ses` client can't be
  created, so no parser, queue, notification Lambda or email is reachable.
- **Read-only data.** `subscriptions` is wrapped in `effects.ReadOnlyTable` (scan only; every
  write method raises) in both modes; the dry-run scan never includes `email`.
- **No fallback.** If the token or any dry-run component is unavailable, the run raises; it
  never falls back to a live path.
- The parser still treats a payload with `"dry_run": true` as dry (log only, `ssm` only); a
  payload can turn dry run on, never off.

Regression tests (`DryRunSafetyTests`) run the handlers against a boto3 stand-in whose `lambda`
invoke runs the **real M1 parser** with a recording SQS client: dry run produces a report with
matches and zero Lambda clients, zero SQS messages and scan-only table calls; the same harness
in live mode does produce SQS messages (control).

### One `as_of` per run (month boundary)

The wrapper fixes `as_of` once (UTC instant; months use the Asia/Taipei calendar) and passes it
in every payload with the resolved `month`. The parser applies rolling-month and expiry rules at
that `as_of`, not at its own clock, so a run that starts at 23:59:59 on the last day of a month
and whose parser runs after midnight still matches rolling rows to the wrapper's month — no
skipped and no duplicated month. The parser refuses an `as_of` older than 24 hours or more than
10 minutes in the future, and a month already over at `as_of`. Payloads without `as_of` (the M1
wrapper) fall back to the parser's clock, as before.

### Bounded rotation (no starvation)

With more work items than the cap, the sorted items are split into `w = ceil(n / cap)` windows
and run number `floor(as_of / SCHEDULE_PERIOD_MINUTES)` picks window `run mod w`. Every work item
is checked once per `w` consecutive scheduled runs (e.g. 1,200 items at cap 500 → every item
within 3 runs, 90 minutes at the M1 rate). Stateless, deterministic, no new AWS resource. The
wrapper logs which window ran. Assumption: `SCHEDULE_PERIOD_MINUTES` matches the real schedule;
if runs were systematically skipped in a pattern that shares a factor with `w`, some windows
could wait longer, so the period setting is part of the deployment checklist.

### Offer validation

Both APIs: price is a positive number ≤ 2,000,000; currency (response and item, when present)
equals the requested one; `return_at` is present (round trip only); both dates parse; departure
falls in the requested month and hasn't happened yet; return is on or after departure and
within 366 days; v1 `expires_at` is in the future; returned `origin` / `destination` codes match
the request; `origin_airport` / `destination_airport` (v3) belong to the requested code's
airports in `airports.json` (e.g. `TPE` → `TPE` or `TSA`). Rejections are counted per reason and
logged; a response with no valid offer is `no_data`, never an alert.

**Known limitation — alternate airports (kept strict on purpose).** A destination listed as a
single airport accepts only that airport, so an offer to another airport serving the same city
is rejected (`destination_airport_unexpected`): a possible missed alert, never a false one.
Confirmed offline 2026-11-09 for **Venice VCE / Treviso TSF**, **Oslo OSL / Torp TRF**,
**Kuala Lumpur KUL / Subang SZB** and **Nagoya NGO / Komaki NKM**. The catalog is not changed
until B5 measures real rejections; review criteria are in `docs/m1.4-b5-live-validation-plan.md`
§6a.

Deliberate differences from M1 (owner review): (1) rolling rows resolve to next month in
**Asia/Taipei** at the wrapper's `as_of` (M1 used UTC at the parser; they differ only
00:00–08:00 Taipei on the 1st of a month); (2) the v1 path applies the same offer validation,
so a one-way, wrong-month, expired or wrong-currency offer can no longer trigger an alert.

### DRY_RUN instructions

Offline (no AWS, no network) — the wrapper's in-process dry-run path on fixtures:

```
python3 aws/tools/farecheck_dry_run.py --api v1     # or --api v3
python3 aws/tools/farecheck_dry_run.py --api v3 --now 2026-11-06T13:00:00Z \
    --subscriptions my-rows.json --responses my-recorded-responses/
```

Fixtures live in `aws/tests/fixtures/travelpayouts/` (`<api>_<origin>_<dest>_<month>_<cur>.json`,
synthetic, documented field names). A missing response file behaves like an unreachable API.
Tests: `python3 -m unittest aws/tests/test_m14_fare_check.py`.

Live Travelpayouts comparison (B5 evidence) needs **separate approval** and follows the local,
read-only plan in `docs/m1.4-b5-live-validation-plan.md` (no AWS at all).

### AWS resources needed later (each needs approval; none created)

1. **Packaged deployment** of `flight-parser` and `flight-parser-wrapper` (handlers
   `parser.handler` / `wrapper.handler`, code from this ZIP instead of inline `ZipFile`), i.e. a
   template change to `flight-m1-fetch-prices` and an artifact location (planned S3 artifacts
   bucket, ADR D10). Both functions are replaced in the same change set.
2. **Environment variables** on those functions: `FARECHECK_MODE=dry_run` first (explicitly;
   there is no default), `FARE_API=v1`, `SCHEDULE_PERIOD_MINUTES` equal to the schedule.
3. **Wrapper timeout** long enough for an in-process dry run (`DRY_RUN_MAX_ITEMS` lookups, about
   1 s each; e.g. 300 s for the default 50 with USD), and **parser reserved concurrency ≈ 5**.
4. IAM: none for the runtime — `flight-lambda-role` already allows `ssm:GetParameter` on
   `/flight/*`, `dynamodb:Scan` on `subscriptions`, `sqs:SendMessage` on `flight-fare-queue` and
   `lambda:InvokeFunction` on `flight-parser` (checked read-only 2026-11-06). The shared role is
   broader than needed (it can also write both tables); the code is read-only by construction
   (`ReadOnlyTable`), and a least-privilege split is recommended later.
5. Optional, later: the `fare_check` table (key `"{origin}-{destination}#{month}"`, TTL on
   `expires_at`, records from `farecheck/status.py`) with `PutItem` for the parser and
   `BatchGetItem` for the API; the `route-index` GSI at ~1,000 subscriptions.
