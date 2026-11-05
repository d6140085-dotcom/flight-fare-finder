# Deployment design: GitHub Actions + AWS OIDC

**Status:** design only. Nothing here exists in AWS or in `.github/workflows/` yet. Creating
it is a separate, approved step (Phase 1 prerequisite).

## Goals

- No long-lived AWS credentials anywhere (GitHub, laptops, chat).
- Every production change is a CloudFormation **change set** that a human reviews and
  approves in GitHub before it runs.
- No pipeline role can create IAM principals, read secrets, delete tables or purge queues.
- Lambda code is shipped as S3 artifacts (no 4,096-byte inline limit) and verified after deploy.

## Components (one-time bootstrap, done manually by the owner)

| Component | Purpose | Policy file |
|---|---|---|
| IAM OIDC provider `token.actions.githubusercontent.com` (audience `sts.amazonaws.com`) | Lets GitHub workflows request short-lived AWS credentials | — |
| Role `flight-gha-plan` | Runs on PRs and `main`: validate, upload artifacts, create/describe/delete change sets. **Explicitly denied** `ExecuteChangeSet`, `CreateStack`, `UpdateStack`, `DeleteStack`. | `aws/iam/gha-plan-trust.json`, `aws/iam/gha-plan-policy.json` |
| Role `flight-gha-deploy` | Assumable **only** by jobs in the `production` GitHub environment. Can execute an existing change set; denied from creating one or editing stacks directly. | `aws/iam/gha-deploy-trust.json`, `aws/iam/gha-deploy-policy.json` |
| Role `flight-cfn-exec` | The CloudFormation service role. Limited to `flight-*` Lambdas, event-source mappings, HTTP APIs, `/aws/lambda/flight-*` log groups and `flight-*` rules. May create (never update or delete) the `subscription_quota` table (M1.4). Denied: IAM writes, SSM reads/writes, `DeleteTable`/`UpdateTable`/`DeleteItem`, `PurgeQueue`/`DeleteQueue`. | `aws/iam/cfn-exec-trust.json`, `aws/iam/cfn-exec-policy.json` |
| Role `flight-subscriptions-api-role` | Runtime role for the M1.4 API Lambda: `subscriptions` and `subscription_quota` tables only. | `aws/iam/subscriptions-api-role-*.json` |
| S3 bucket `flight-artifacts-<account>` | Versioned, private, SSE, public access blocked. Holds `artifacts/<function>/<git-sha>.zip`. | — |
| GitHub environment `production` | Required reviewer: the repository owner. Deployment branch: `main` only. | — |

Trust policies pin `aud = sts.amazonaws.com` and `sub` to this repository
(`repo:d6140085-dotcom/flight-fare-finder:…`). The deploy role accepts only
`…:environment:production`.

## Workflow (to be added as `.github/workflows/deploy.yml` in a later phase)

```yaml
name: aws-deploy
on:
  pull_request:
    paths: ["aws/**"]
  workflow_dispatch:
    inputs:
      change_set: { description: "Reviewed change set name", required: true }
      stack: { description: "Stack name (flight-m1-*)", required: true }

permissions:
  contents: read

jobs:
  plan:                       # PRs: test, build, upload, create change set, report
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    permissions: { contents: read, id-token: write, pull-requests: write }
    steps:
      - uses: actions/checkout@v4
      - run: python3 -m unittest discover -s aws/tests -v
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::379959319681:role/flight-gha-plan
          aws-region: ap-southeast-2
      # build zips -> s3://flight-artifacts-<acct>/artifacts/<fn>/<sha>.zip
      # build templates (Code: S3Bucket/S3Key) -> aws cloudformation create-change-set
      #   --role-arn …/flight-cfn-exec --change-set-name pr-<n>-<sha7>
      # describe-change-set -> PR comment listing every Action/Replacement;
      #   fail the job if any Replacement != False or any Remove action appears.

  deploy:                     # manual: executes exactly the reviewed change set
    if: github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    environment: production   # pauses for the owner's approval
    permissions: { contents: read, id-token: write }
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::379959319681:role/flight-gha-deploy
          aws-region: ap-southeast-2
      # re-describe the change set; abort unless status AVAILABLE and unchanged
      # execute-change-set; wait for UPDATE_COMPLETE
      # verify: drift detection IN_SYNC, Lambda CodeSha256 == built artifact hash,
      #         schedule state unchanged unless the change set says otherwise
```

Action versions are pinned to major tags here; pin to full commit SHAs when the workflow is
created.

## Safety properties

- **Fork PRs** don't receive an OIDC token by default, and even the plan role can't execute.
- **Approval is about a specific change set.** The deploy job executes a named, already
  reviewed change set and refuses if it changed.
- **The schedule can't be switched on by accident.** `ScheduleState` defaults to `DISABLED`
  and only an explicit parameter in a reviewed change set changes it.
- **Verifiable code.** With S3 artifacts, `CodeSha256` equals the SHA-256 of the zip the
  workflow built, so deployments are checked byte-for-byte. This replaces reading code back
  through the AWS connector, which mangles non-ASCII text.

## Migration of existing stacks

Each `flight-m1-*` stack moves from inline `ZipFile` to `S3Bucket/S3Key` in its own reviewed
change set (an in-place `Modify` of `Code`, no replacement). The frozen M1 stacks move last,
after the M1 email test and only with explicit approval.

## Prerequisites and notes

- The GitHub App used by Claude sessions needs the `workflows` permission to push files under
  `.github/workflows/`; otherwise the owner adds the workflow file.
- Bootstrap IAM is created once by the owner (or by Claude with explicit approval); the
  pipeline itself can never create or widen IAM.
