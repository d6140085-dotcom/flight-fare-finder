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

### Affiliate marker (not enabled)

To earn commission, add your Travelpayouts partner ID ("marker", not a secret) as
`?marker=<id>` on the booking link, e.g. `https://www.aviasales.com/search/TPE0212TYO09121?marker=<id>`.
Suggested wiring: a non-secret env var `TP_MARKER` on `flight-fare-notification` (set from the
template builder), appended only when set. The Travelpayouts API token is unrelated and must
never appear in links. Moving the code off the inline 4,096-byte deployment is needed first.

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
| `wrapper.handler` | `flight-parser-wrapper` | Scans `subscriptions` **without `email`** (route, origin, destination, departure_month, alerts_paused), drops paused/expired/unknown rows, and invokes the parser once per distinct `(origin, destination, effective_month)`; `flight-routes.json` is no longer read |
| `parser.handler` | `flight-parser` | One work item → Travelpayouts (v1 or v3) → validated cheapest round-trip TWD fare → rows of that route **and month**, active, target ≥ fare → SQS (M1 message shape, key for key), or log only in DRY_RUN |

Package layout (`farecheck/`): `workitems.py` (rows → work items), `months.py` (same rules as
the API; parity-tested), `travelpayouts.py` (v1/v3 adapters and offer validation), `matching.py`,
`status.py` (fare-check records, API contract §3.4), `compat.py` (v1/v3 comparison for B5),
`effects.py` (the only place with side effects), `runner.py`.

Settings (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `FARE_API` | `v1` | `v1` = M1's `/v1/prices/cheap` query; `v3` = `/aviasales/v3/prices_for_dates` with `one_way=false`, `currency=twd`, `sorting=price`, `limit=V3_LIMIT`, no `market` |
| `DRY_RUN` | off | Any value except `0/false/no/off` turns it on. Parser: no SQS client is ever created; would-be messages are logged with a subscriber hash. Wrapper: every parser payload carries `"dry_run": true`, and the parser honours a payload flag that turns DRY_RUN **on** (never off) |
| `FETCH_USD` | on | M1's best-effort USD lookup for the 約 US$ line; `0` halves Travelpayouts calls |
| `V3_LIMIT` | `30` | Offers per v3 request (documented default 30, max 1000) |
| `MAX_WORK_ITEMS` | `500` | Safety cap per run; extra items are logged and wait for the next run |
| `FARE_QUEUE_URL`, `TP_PARAM`, `PARSER_FUNCTION` | as M1 | Unchanged |

Offer validation (both APIs): price is a positive number ≤ 2,000,000; currency (response and
item, when present) equals the requested one; `return_at` is present (round trip only); both
dates parse; departure falls in the requested month and hasn't happened yet; return is on or
after departure and within 366 days; v1 `expires_at` is in the future; returned `origin` /
`destination` codes match the request; `origin_airport` / `destination_airport` (v3) belong to
the requested city's airports in `airports.json` (e.g. `TPE` → `TPE` or `TSA`). Rejections are
counted per reason and logged; a response with no valid offer is `no_data`, never an alert.

Deliberate differences from M1 (owner review): (1) rolling rows resolve to next month in
**Asia/Taipei** (M1 used UTC; they differ only 00:00–08:00 Taipei on the 1st of a month);
(2) the v1 path now applies the same offer validation, so a one-way, wrong-month, expired or
wrong-currency offer can no longer trigger an alert.

### DRY_RUN instructions

Offline (no AWS, no network) — wrapper and parser in-process on fixtures:

```
python3 aws/tools/farecheck_dry_run.py --api v1     # or --api v3
python3 aws/tools/farecheck_dry_run.py --api v3 --now 2026-11-06T13:00:00Z \
    --subscriptions my-rows.json --responses my-recorded-responses/
```

Fixtures live in `aws/tests/fixtures/travelpayouts/` (`<api>_<origin>_<dest>_<month>_<cur>.json`,
synthetic, documented field names). A missing response file behaves like an unreachable API.
Tests: `python3 -m unittest aws/tests/test_m14_fare_check.py`.

Live DRY_RUN against Travelpayouts (B5 evidence) needs **separate approval**. Proposed shape:
a temporary isolated stack with two DRY_RUN-only functions from this ZIP, a role with only
`dynamodb:Scan` on `subscriptions` (read), `ssm:GetParameter` on `/flight/travelpayouts` and
`lambda:InvokeFunction` on its own parser — no SQS, no write, no notification access — run for
several schedules with `FARE_API=v1` and `v3`, then compared with `farecheck/compat.py`.

### AWS resources needed later (each needs approval; none created)

1. **Packaged deployment** of `flight-parser` and `flight-parser-wrapper` (handlers
   `parser.handler` / `wrapper.handler`, code from this ZIP instead of inline `ZipFile`), i.e. a
   template change to `flight-m1-fetch-prices` and an artifact location (planned S3 artifacts
   bucket, ADR D10).
2. **Environment variables** on those functions (`FARE_API=v1`, `DRY_RUN=1` first).
3. **Parser reserved concurrency ≈ 5** (Travelpayouts per-minute limits).
4. IAM: none for the runtime — `flight-lambda-role` already allows `dynamodb:Scan` on
   `subscriptions`, `sqs:SendMessage` on `flight-fare-queue` and `lambda:InvokeFunction` on
   `flight-parser` (checked read-only 2026-11-06). The shared role is broader than needed
   (it can also write both tables); a dedicated read-only role is recommended for the live
   DRY_RUN stack above and for a later least-privilege split.
5. Optional, later: the `fare_check` table (key `"{origin}-{destination}#{month}"`, TTL on
   `expires_at`, records from `farecheck/status.py`) with `PutItem` for the parser and
   `BatchGetItem` for the API; the `route-index` GSI at ~1,000 subscriptions.
