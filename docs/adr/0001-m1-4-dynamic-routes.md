# ADR 0001 — M1.4 user-defined routes

- **Status:** Accepted for Phase 0 (design only). Phases 1–5 each need separate approval.
- **Date:** 2026-11-04
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

### D1. Keep the `subscriptions` table and keys
Key stays `email` (partition) + `route` (sort, `ORIG-DEST`). One subscription per user per
route; "multiple subscriptions" means multiple routes. No migration: new attributes are
optional and absent values have safe defaults.

New attributes: `alerts_paused` (bool, absent = active), `route_type` and display names are
derived from the allowlist, `plan_name` becomes optional (kept on the two M1 rows),
`previous_route` (set when a route is changed, for audit).

### D2. Pause is `alerts_paused`, never `subscription_status`
`subscription_status` is reserved for M2's payment paywall. "Paused by the user" and "not paid"
are independent; from M2 an alert requires `not alerts_paused AND subscription_status = active`.

### D3. Curated, file-driven allowlist
`aws/config/airports.json` (schema_version 1): origins TPE, KHH, RMQ; 25 Asian destinations.
Each entry has `code`, `type` (`city` = IATA metro code such as TYO, or `airport`),
`name_zh`, `name_en`, `country`. The API validates against it server-side; the front end
reads the same file at build time. Expanding = editing the file (no schema change).

### D4. Configurable active-subscription limit
`aws/config/subscription-policy.json`: `max_active_subscriptions_per_user = 10`, passed to the
API as an environment variable. Paused subscriptions don't count; resuming re-checks the limit.
Enforcement (updated 2026-11-05, owner decision: **strict**): a per-user record in a separate
`subscription_quota` table is updated in the same transaction as every write that changes the
active count, guarded by a version condition, so concurrent requests can't exceed the limit.
Details: `docs/api/m1.4-subscriptions-api.md` §5.

### D5. Route changes are a transaction
Changing origin/destination changes the sort key, so it is one `TransactWriteItems`:
`Delete(old, condition attribute_exists)` + `Put(new, condition attribute_not_exists)`.
Duplicates are impossible (the `Put` condition fails → `409 duplicate_subscription`);
a vanished source row → `404`; a stale client (`if_updated_at` mismatch) → `409 conflict`.
Creating uses `PutItem` with `attribute_not_exists` for the same reason. Target/pause edits are
single `UpdateItem`s with `attribute_exists`.

### D6. Notification history is never changed by the subscriptions API
History is keyed `{email}#{route}`; the API's IAM role has **no access** to that table.

| User action | Effect on alerts |
|---|---|
| Change target price | Same key; the 24 h floor / re-alert rules still apply to the last alert |
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
must send `one_way=false` explicitly to keep today's round-trip semantics. Neither endpoint is
called with `market` today (default `ru`); that stays unchanged during migration. The switch
is behind a `FARE_API=v1|v3` setting (default v1) and happens only after a dry-run comparison
passes (see the plan, Phase 2).

### D9. Least-privilege roles
A new `flight-subscriptions-api-role` (DynamoDB `subscriptions` only; no SSM, KMS, SQS,
Lambda or `notification_history`). The Travelpayouts and Resend keys stay readable only by
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

## Alternatives considered

- **New table / composite key with a subscription ID** (allow several targets per route):
  more flexible but needs a migration and changes the dedup key. Rejected for M1.4.
- **Counter item inside the `subscriptions` table**: exact, but the deployed M1.1 list Lambda
  and the parser read every row and would break on a non-subscription row. Rejected in favour of
  the separate `subscription_quota` table.
- **Free-text airport search (Travelpayouts autocomplete)**: better UX, but unpopular routes
  often have no cached fares. Deferred until the allowlist proves out.
- **Keep inline CloudFormation code**: no new infrastructure, but the notification code is
  already at the limit and reading code back through the AWS connector is unreliable for
  non-ASCII text. Rejected for the long term.

## Consequences

- No data migration; the two existing subscriptions and all history keep working unchanged.
- The notification Lambda needs no change for new routes (it shows unknown city codes as the
  bare code until `CITIES` is extended, a settings-only change after M1 closes).
- One-time IAM bootstrap (OIDC provider, three roles, artifacts bucket) needs owner approval.
