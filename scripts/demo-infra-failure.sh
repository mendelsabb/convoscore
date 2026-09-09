#!/usr/bin/env bash
# Kill a pod and watch Kubernetes put it back.
#
# This is an operator command, not a button in the UI, and that is deliberate: the application has
# no Kubernetes RBAC at all and no service account token mounted, so it *cannot* delete pods. A
# demo control that could would be a demo control an attacker could use.
#
#   make demo-infra-failure                  # kills one worker
#   make demo-infra-failure TARGET=api       # kills the API
#   make demo-infra-failure TARGET=postgres  # kills the database (results survive; see below)
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

target="${TARGET:-worker}"
cluster_exists || fail "no kind cluster named '$CLUSTER_NAME'. Run: make up"

case "$target" in
  worker|api|ingestor|web|postgres) ;;
  *) fail "unknown target '$target' (worker, api, ingestor, web, postgres)" ;;
esac

if [ "$target" = "postgres" ]; then
  selector="app.kubernetes.io/component=postgres"
  workload="statefulset/${RELEASE_NAME}-postgres"
else
  selector="app.kubernetes.io/component=$target"
  workload="deploy/${RELEASE_NAME}-$target"
fi

step "Before"
kc -n "$NAMESPACE" get pods -l "$selector" -o wide 2>/dev/null | sed 's/^/     /'
restarts_before="$(kc -n "$NAMESPACE" get pods -l "$selector" \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' 2>/dev/null \
  | awk '{ total += $1 } END { print total + 0 }')"

victim="$(kc -n "$NAMESPACE" get pods -l "$selector" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
[ -n "$victim" ] || fail "no $target pod found"

step "Deleting pod $victim"
kc -n "$NAMESPACE" delete pod "$victim" --wait=false >/dev/null
ok "deletion requested"

step "Kubernetes replaces it"
if [ "$target" = "postgres" ]; then
  kc -n "$NAMESPACE" rollout status "$workload" --timeout=180s
else
  kc -n "$NAMESPACE" rollout status "$workload" --timeout=180s
fi

step "After"
kc -n "$NAMESPACE" get pods -l "$selector" -o wide 2>/dev/null | sed 's/^/     /'

printf '\n'
step "What to look at"
printf '     The pod name changed: the old one is gone, a new one took its place.\n'
printf '     Grafana → ConvoScore Overview → Kubernetes row shows the restart and the gap.\n'
printf '     %s\n' "$GRAFANA_URL"
printf '\n'
if [ "$target" = "worker" ]; then
  printf '     Nothing was lost. A job the dead worker had claimed is redelivered by SQS once its\n'
  printf '     visibility timeout expires, and the surviving worker picks it up.\n'
elif [ "$target" = "postgres" ]; then
  printf '     Results survived: the data is on a PersistentVolumeClaim, not in the pod.\n'
  printf '     Confirm with: curl -s %s/api/stats\n' "$API_URL"
else
  printf '     Requests are served again as soon as the new pod passes its readiness probe.\n'
fi
printf '\n'
printf '     Restart count for %s before this run: %s\n' "$target" "$restarts_before"
printf '     Deleting a pod does not increment restartCount; Kubernetes creates a new pod.\n'
printf '     For a container restart instead, see: make demo-restart\n'
