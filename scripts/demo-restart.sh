#!/usr/bin/env bash
# Prove that results survive a full restart of everything.
#
# Reads the job counts, restarts every application component, deletes the database pod, waits for
# the system to come back, and reads the counts again. Same numbers means the results live in
# PostgreSQL on a PersistentVolumeClaim rather than in any pod.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

cluster_exists || fail "no kind cluster named '$CLUSTER_NAME'. Run: make up"
curl -sf "$API_URL/readyz" >/dev/null 2>&1 || fail "the API is not ready. Run: make status"

snapshot() {
  curl -s "$API_URL/api/stats" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("unavailable")
    raise SystemExit
s = d["by_status"]
print("%s total (%s completed, %s failed) | %s tokens | $%s estimated" % (
    d["total_jobs"], s["completed"], s["failed"], d["total_tokens"], d["estimated_cost_usd"]))
' 2>/dev/null || echo "unavailable"
}

step "Before the restart"
before="$(snapshot)"
printf '     %s\n' "$before"
if [ "$before" = "0 total (0 completed, 0 failed) | 0 tokens | \$0 estimated" ]; then
  warn "there are no results to lose yet. Run 'make demo-data' first for a meaningful demo."
fi

step "Restarting every application component"
kc -n "$NAMESPACE" rollout restart \
  deploy/"${RELEASE_NAME}"-api \
  deploy/"${RELEASE_NAME}"-worker \
  deploy/"${RELEASE_NAME}"-ingestor \
  deploy/"${RELEASE_NAME}"-web >/dev/null
ok "rollout requested"

step "Deleting the PostgreSQL pod"
kc -n "$NAMESPACE" delete pod "${RELEASE_NAME}-postgres-0" --wait=false >/dev/null
ok "deletion requested"

step "Waiting for everything to come back"
kc -n "$NAMESPACE" rollout status deploy/"${RELEASE_NAME}"-api --timeout=240s
kc -n "$NAMESPACE" rollout status deploy/"${RELEASE_NAME}"-worker --timeout=240s
kc -n "$NAMESPACE" wait --for=condition=ready pod "${RELEASE_NAME}-postgres-0" --timeout=240s >/dev/null
printf '     waiting for the API'
wait_for 180 curl -sf "$API_URL/readyz" || fail "the API did not come back"
printf '\n'

step "After the restart"
after="$(snapshot)"
printf '     %s\n' "$after"

printf '\n'
if [ "$before" = "$after" ]; then
  ok "identical. Every result survived a full restart."
  printf '     The data is on a PersistentVolumeClaim, so it outlives every pod that touched it.\n'
  printf '     Only "make down" (which deletes the cluster) removes it.\n'
else
  warn "the counts differ. If a job was still being scored during the restart, it will have"
  warn "finished afterwards, which changes completed counts legitimately. Re-run when idle."
fi
printf '\n'
printf '     Browse the results: %s/conversations\n' "$WEB_URL"
