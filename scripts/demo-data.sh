#!/usr/bin/env bash
# Feed the demo conversations through both ingestion paths.
#
# API conversations are posted straight to the service. Storage conversations are uploaded to
# LocalStack from inside the cluster, using the boto3 already present in the application image, so
# no AWS CLI or Python environment is needed on the reviewer's machine.
#
# Scores are never hardcoded: every conversation goes through the configured scoring backend.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

cluster_exists || fail "no kind cluster named '$CLUSTER_NAME'. Run: make up"
curl -sf "$API_URL/readyz" >/dev/null 2>&1 || fail "the API is not ready. Run: make status"

# --- direct API submissions ---------------------------------------------------------------
step "Submitting conversations through the API"
submitted=0
for fixture in "$REPO_ROOT"/demo/fixtures/api/*.json; do
  name="$(basename "$fixture")"
  response="$(curl -s -w '\n%{http_code}' -X POST "$API_URL/api/conversations" \
    -H 'Content-Type: application/json' --data-binary "@$fixture")"
  code="$(printf '%s' "$response" | tail -n 1)"
  body="$(printf '%s' "$response" | sed '$d')"
  if [ "$code" = "202" ]; then
    job_id="$(printf '%s' "$body" | sed -E 's/.*"job_id":"([^"]+)".*/\1/')"
    printf '     %-34s -> %s\n' "$name" "$job_id"
    submitted=$((submitted + 1))
  else
    warn "$name rejected (HTTP $code): $body"
  fi
done
ok "$submitted conversations submitted"

# --- storage ingestion --------------------------------------------------------------------
step "Uploading conversations to object storage"
ingestor_pod="$(kc -n "$NAMESPACE" get pod \
  -l app.kubernetes.io/component=ingestor \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
[ -n "$ingestor_pod" ] || fail "no ingestor pod found"

uploaded=0
for fixture in "$REPO_ROOT"/demo/fixtures/s3/*.json; do
  name="$(basename "$fixture")"
  # The object body is piped in on stdin so nothing has to be copied into the pod first.
  if kc -n "$NAMESPACE" exec -i "$ingestor_pod" -- python -c '
import os, sys, boto3
key = os.environ["S3_PREFIX"] + sys.argv[1]
boto3.client(
    "s3",
    endpoint_url=os.environ["AWS_ENDPOINT_URL"],
    region_name=os.environ["AWS_REGION"],
).put_object(Bucket=os.environ["S3_BUCKET"], Key=key, Body=sys.stdin.buffer.read())
print(key)
' "$name" < "$fixture" >/dev/null 2>&1; then
    printf '     %-34s -> uploaded\n' "$name"
    uploaded=$((uploaded + 1))
  else
    warn "could not upload $name"
  fi
done
ok "$uploaded objects uploaded (the ingestor picks them up within a poll interval)"

printf '\n'
step "Watching progress"
printf '     Conversations are scored asynchronously. Poll with:\n'
printf '       curl -s %s/api/stats\n' "$API_URL"
printf '       make status\n'
printf '     Uploading the same objects again is a no-op: duplicates are skipped by design.\n'
