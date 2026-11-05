#!/usr/bin/env bash
# Phase 1B change-set PREPARATION for stack flight-m1-4-subscriptions-api.
#
# Default (no flags): offline dry run — runs the tests, builds the ZIP and the template, and
# prints the AWS commands it *would* run. Contacts nothing.
#
# --create-change-set: additionally uploads the ZIP to the artifacts bucket and creates a
# change set for review. It NEVER executes the change set; executing is a separate, approved
# step (GitHub "production" environment or the owner). Requires CONFIRM=create-change-set.
#
# Prerequisites for --create-change-set (one-time bootstrap, owner-approved): artifacts bucket
# flight-artifacts-<account>, roles flight-cfn-exec and flight-subscriptions-api-role
# (aws/iam/*.json), AWS credentials for flight-gha-plan.
set -euo pipefail
ACCOUNT="379959319681"
REGION="ap-southeast-2"
STACK="flight-m1-4-subscriptions-api"
MODE="${1:-}"
cd "$(dirname "$0")/../.."

: "${SUPABASE_URL:?set SUPABASE_URL}"
: "${SUPABASE_PUBLISHABLE_KEY:?set SUPABASE_PUBLISHABLE_KEY (sb_publishable_...)}"

python3 -m unittest discover -s aws/tests >/dev/null 2>&1 || { echo "tests failed" >&2; exit 1; }
pkg_json="$(python3 aws/scripts/package_subscriptions_api.py aws/build)"
zip_path="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["zip"])' <<<"$pkg_json")"
code_sha="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["code_sha256"])' <<<"$pkg_json")"
sha_hex="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["sha256"])' <<<"$pkg_json")"
key="artifacts/flight-subscriptions-api/${sha_hex}.zip"
python3 aws/stacks/build_m14_subscriptions_api.py "$ACCOUNT" "$key" "$code_sha" > aws/build/m14-subscriptions-api.json
change_set="m14-1b-${sha_hex:0:12}"

cmds=(
  "aws s3 cp $zip_path s3://flight-artifacts-$ACCOUNT/$key --region $REGION"
  "aws cloudformation create-change-set --region $REGION --stack-name $STACK --change-set-type CREATE --change-set-name $change_set --template-body file://aws/build/m14-subscriptions-api.json --role-arn arn:aws:iam::$ACCOUNT:role/flight-cfn-exec --tags Key=project,Value=flight-price-notifier"
  "aws cloudformation wait change-set-create-complete --region $REGION --stack-name $STACK --change-set-name $change_set"
  "aws cloudformation describe-change-set --region $REGION --stack-name $STACK --change-set-name $change_set"
)
echo "ZIP: $zip_path  sha256=$sha_hex  CodeSha256=$code_sha"
echo "Template: aws/build/m14-subscriptions-api.json"

if [[ "$MODE" != "--create-change-set" ]]; then
  echo "Dry run. Would run:"; printf '  %s\n' "${cmds[@]}"
  echo "Never run by this script: aws cloudformation execute-change-set (owner-approved step)."
  exit 0
fi
[[ "${CONFIRM:-}" == "create-change-set" ]] || { echo "Refusing: set CONFIRM=create-change-set" >&2; exit 1; }
[[ "$(aws sts get-caller-identity --query Account --output text)" == "$ACCOUNT" ]] || { echo "wrong account" >&2; exit 1; }
for c in "${cmds[@]}"; do echo "+ $c"; eval "$c"; done
echo "Change set $change_set created for review. NOT executed."
