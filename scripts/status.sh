#!/usr/bin/env bash
# Show what is running, where to reach it, and whether it is healthy.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

brief=0
[ "${1:-}" = "--brief" ] && brief=1

if ! cluster_exists; then
  warn "no kind cluster named '$CLUSTER_NAME'. Run: make up"
  exit 0
fi

if [ "$brief" -eq 0 ]; then
  step "Pods"
  kc -n "$NAMESPACE" get pods 2>/dev/null || warn "namespace $NAMESPACE not found"
  kc -n "$LOCALSTACK_NAMESPACE" get pods 2>/dev/null || true
  printf '\n'
fi

step "URLs"
printf '     API and Swagger docs   %s/docs\n' "$API_URL"
printf '     LocalStack (S3, SQS)   %s\n' "$LOCALSTACK_ENDPOINT"
printf '     Review UI              http://127.0.0.1:8080  (milestone 6)\n'
printf '     Grafana                http://127.0.0.1:3000  (milestone 7)\n'
printf '\n'

# The JSON summaries below are formatted by python3 when it is available.
#
# `python3 -c`, not `python3 - <<EOF`: with a heredoc the program itself arrives on stdin, so the
# piped JSON would never reach the script. The programs use only double quotes inside a
# single-quoted shell string, and only old syntax, because the python3 on a reviewer's macOS is
# still 3.9.
render_health() {
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for dependency in data.get("dependencies", []):
    mark = "ok" if dependency.get("healthy") else "DOWN"
    print("   %4s %s: %s" % (mark, dependency.get("name"), dependency.get("detail", "")))
config = data.get("config", {})
print("     scoring: %s (%s)   prompt %s   demo_mode %s" % (
    config.get("llm_provider"), config.get("model"),
    config.get("prompt_version"), config.get("demo_mode")))
' 2>/dev/null || true
}

render_stats() {
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
status = data.get("by_status", {})
print("     total %s   pending %s   processing %s   completed %s   failed %s" % (
    data.get("total_jobs", 0), status.get("pending", 0), status.get("processing", 0),
    status.get("completed", 0), status.get("failed", 0)))
print("     by source: %s" % (data.get("by_source") or "none yet"))
print("     tokens %s   estimated cost $%s" % (
    data.get("total_tokens", 0), data.get("estimated_cost_usd", "0")))
' 2>/dev/null || true
}

step "Health"
if curl -sf "$API_URL/readyz" >/dev/null 2>&1; then
  ok "API is ready"
  if have python3; then
    curl -s "$API_URL/api/health/details" 2>/dev/null | render_health
  fi
else
  warn "the API is not answering on $API_URL"
  printf '     Try: kubectl --context kind-%s -n %s get pods\n' "$CLUSTER_NAME" "$NAMESPACE"
fi

if [ "$brief" -eq 0 ] && have python3; then
  printf '\n'
  step "Jobs"
  curl -s "$API_URL/api/stats" 2>/dev/null | render_stats
fi

printf '\n'
printf '     make demo-data   submit sample conversations through both ingestion paths\n'
printf '     make logs COMPONENT=worker\n'
