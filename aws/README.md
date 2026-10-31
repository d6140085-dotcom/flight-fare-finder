# AWS back end (M1)

Region **ap-southeast-2**, account resources shared by all M1 Lambdas via the existing
`flight-lambda-role`. Secrets live in **SSM Parameter Store** (SecureString), never in code or logs:
`/flight/travelpayouts`, `/flight/resend`, `/github/pat`.

## Part 1.2 — fetch prices on a schedule

| Resource | Name | Managed by |
|---|---|---|
| S3 bucket (private, SSE-S3) | `flight-config-<account>` holding `flight-routes.json` | created directly |
| SQS queue (SSE-SQS) | `flight-fare-queue` | created directly (Part 1.3 sets its visibility timeout) |
| Role policy statements | `ReadRoutesConfig`, `EnqueueFareMatches`, `FanOutToParser` in `flight-data` | merged into the existing inline policy |
| Lambdas | `flight-parser`, `flight-parser-wrapper` | CloudFormation stack `flight-m1-fetch-prices` |
| EventBridge rule | `flight-price-check`, `rate(30 minutes)` → wrapper | same stack |
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
4. Update the stack `flight-m1-fetch-prices` with that template.

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

While `from` is `onboarding@resend.dev`, Resend only delivers to the Resend account's own
address (others get `403`; `example.com` addresses get `422`). Verify a domain (M3) to email anyone else.

Redeploy: `python3 aws/stacks/build_m1_email_on_target.py <account-id> > template.json`, then
update stack `flight-m1-email-on-target`. After changing `/flight/resend`, change any env var on
the function to force a cold start (the parameter is cached per container).
