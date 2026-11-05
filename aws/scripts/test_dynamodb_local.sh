#!/usr/bin/env bash
# Run the M1.4 subscriptions API tests (incl. the >= 50-request concurrency suite) against
# Amazon DynamoDB Local in Docker. Local only: no AWS account, fake credentials, localhost.
# Requires Docker and: pip install -r aws/requirements-test.txt
set -euo pipefail
IMAGE="${DYNAMODB_LOCAL_IMAGE:-amazon/dynamodb-local:2.6.0}"
PORT="${DYNAMODB_LOCAL_PORT:-8000}"
cd "$(dirname "$0")/../.."
name="flight-ddb-local-$$"
docker run -d --rm --name "$name" -p "127.0.0.1:${PORT}:8000" "$IMAGE" -jar DynamoDBLocal.jar -inMemory >/dev/null
trap 'docker stop "$name" >/dev/null' EXIT
for _ in $(seq 1 30); do
  curl -s -o /dev/null "http://127.0.0.1:${PORT}" && break
  sleep 1
done
DDB_ENDPOINT="http://127.0.0.1:${PORT}" AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \
  AWS_DEFAULT_REGION=ap-southeast-2 python3 -m unittest -v aws/tests/test_m14_subscriptions_api.py
