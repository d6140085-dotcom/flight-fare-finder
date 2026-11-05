#!/usr/bin/env bash
# Phase 1B change-set PREPARATION for stack flight-m1-4-subscriptions-api.
#
# Default (no flags): offline dry run. Runs the tests, builds the ZIP and the template, and
# prints the AWS commands it *would* run. Contacts nothing.
#
# --create-change-set: additionally uploads the ZIP (write-once) and creates a change set for
# review. Requires CONFIRM=create-change-set. This script contains NO command that executes a
# change set or creates/updates/deletes a stack; executing is a separate, owner-approved step
# (GitHub "production" environment). The flight-gha-plan role it is meant to run as is also
# explicitly denied cloudformation:ExecuteChangeSet (aws/iam/gha-plan-policy.json).
#
# Prerequisites for --create-change-set (one-time, owner-approved bootstrap): artifacts bucket
# flight-artifacts-<account> (versioned), roles flight-cfn-exec and flight-subscriptions-api-role.
set -euo pipefail
ACCOUNT="379959319681"
REGION="ap-southeast-2"
STACK="flight-m1-4-subscriptions-api"
BUCKET="flight-artifacts-${ACCOUNT}"
MODE="${1:-}"
cd "$(dirname "$0")/../.."

: "${SUPABASE_URL:?set SUPABASE_URL}"
: "${SUPABASE_PUBLISHABLE_KEY:?set SUPABASE_PUBLISHABLE_KEY (sb_publishable_...)}"

# The artifact must be exactly a commit: refuse uncommitted changes.
# (Tracked changes anywhere, or untracked files in anything that goes into the ZIP/template.)
if [[ -n "$(git status --porcelain --untracked-files=no)" ]] || \
   [[ -n "$(git status --porcelain -- aws/lambdas/flight-subscriptions-api aws/config aws/stacks aws/scripts)" ]]; then
  echo "Refusing: uncommitted or untracked changes. The artifact must match a commit." >&2
  exit 1
fi
COMMIT="$(git rev-parse HEAD)"

python3 -m unittest discover -s aws/tests >/dev/null 2>&1 || { echo "tests failed" >&2; exit 1; }
pkg_json="$(python3 aws/scripts/package_subscriptions_api.py aws/build)"
field() { python3 -c "import json,sys;print(json.load(sys.stdin)[sys.argv[1]])" "$1" <<<"$pkg_json"; }
ZIP="$(field zip)"
SHA_HEX="$(field sha256)"
CODE_SHA="$(field code_sha256)"
KEY="artifacts/flight-subscriptions-api/${COMMIT}/${SHA_HEX}.zip"
TEMPLATE="aws/build/m14-subscriptions-api.json"
python3 aws/stacks/build_m14_subscriptions_api.py "$ACCOUNT" "$COMMIT" "$SHA_HEX" "$CODE_SHA" > "$TEMPLATE"
CHANGE_SET="m14-1b-${COMMIT:0:12}"

echo "Commit:   $COMMIT"
echo "ZIP:      $ZIP  sha256=$SHA_HEX  CodeSha256=$CODE_SHA"
echo "Key:      s3://$BUCKET/$KEY"
echo "Template: $TEMPLATE  (no API routes)"

upload() {
  # Write-once: --if-none-match '*' fails if the key exists; then the existing object must have
  # the same SHA-256, or we stop. Keys are never overwritten.
  if ! aws s3api put-object --region "$REGION" --bucket "$BUCKET" --key "$KEY" --body "$ZIP" \
      --checksum-algorithm SHA256 --checksum-sha256 "$CODE_SHA" --if-none-match '*' \
      --metadata "git-commit=$COMMIT,code-sha256=$CODE_SHA" >/dev/null; then
    existing="$(aws s3api head-object --region "$REGION" --bucket "$BUCKET" --key "$KEY" \
      --checksum-mode ENABLED --query ChecksumSHA256 --output text)"
    [[ "$existing" == "$CODE_SHA" ]] || { echo "Existing artifact differs; refusing" >&2; exit 1; }
    echo "Artifact already present with the same SHA-256."
  fi
}

create_change_set() {
  aws cloudformation create-change-set --region "$REGION" --stack-name "$STACK" \
    --change-set-type CREATE --change-set-name "$CHANGE_SET" --template-body "file://$TEMPLATE" \
    --role-arn "arn:aws:iam::${ACCOUNT}:role/flight-cfn-exec" \
    --tags "Key=project,Value=flight-price-notifier" "Key=git_commit,Value=$COMMIT" >/dev/null
  aws cloudformation wait change-set-create-complete --region "$REGION" --stack-name "$STACK" \
    --change-set-name "$CHANGE_SET"
  aws cloudformation describe-change-set --region "$REGION" --stack-name "$STACK" \
    --change-set-name "$CHANGE_SET" --query 'Changes[].ResourceChange.[Action,LogicalResourceId,ResourceType,Replacement]' \
    --output table
}

if [[ "$MODE" != "--create-change-set" ]]; then
  echo "Dry run: nothing was contacted. With --create-change-set and CONFIRM=create-change-set it would:"
  echo "  1. aws s3api put-object (write-once, SHA-256 checked) -> s3://$BUCKET/$KEY"
  echo "  2. aws cloudformation create-change-set --stack-name $STACK --change-set-name $CHANGE_SET (CREATE)"
  echo "  3. wait and describe the change set for review"
  echo "It never executes the change set."
  exit 0
fi
[[ "${CONFIRM:-}" == "create-change-set" ]] || { echo "Refusing: set CONFIRM=create-change-set" >&2; exit 1; }
[[ "$(aws sts get-caller-identity --query Account --output text)" == "$ACCOUNT" ]] || { echo "Wrong AWS account" >&2; exit 1; }
upload
create_change_set
echo "Change set $CHANGE_SET created for review. NOT executed."
