#!/usr/bin/env bash
# Tail one component's logs. COMPONENT=api|worker|ingestor|postgres
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

component="${COMPONENT:-worker}"
cluster_exists || fail "no kind cluster named '$CLUSTER_NAME'. Run: make up"

case "$component" in
  api|worker|ingestor|postgres) ;;
  localstack)
    exec kc -n "$LOCALSTACK_NAMESPACE" logs -l app.kubernetes.io/name=localstack --tail=100 -f
    ;;
  *) fail "unknown component '$component' (api, worker, ingestor, postgres, localstack)" ;;
esac

step "Logs for $component (ctrl-c to stop)"
exec kc -n "$NAMESPACE" logs \
  -l "app.kubernetes.io/component=$component" \
  --all-containers --prefix --tail=100 -f
