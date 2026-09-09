#!/usr/bin/env bash
# Shared helpers. Sourced by the other scripts; not executable on its own.
#
# All logic lives in these scripts rather than the Makefile because macOS ships GNU make 3.81 and
# bash 3.2, where multi-line recipes and modern bash features are unavailable.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO_ROOT

CLUSTER_NAME="${CLUSTER_NAME:-convoscore}"
NAMESPACE="${NAMESPACE:-convoscore}"
RELEASE_NAME="${RELEASE_NAME:-convoscore}"
LOCALSTACK_NAMESPACE="localstack"
MONITORING_NAMESPACE="monitoring"
# Pinned so a reviewer gets the same stack we tested against.
PROMETHEUS_CHART_VERSION="${PROMETHEUS_CHART_VERSION:-29.27.2}"
GRAFANA_CHART_VERSION="${GRAFANA_CHART_VERSION:-10.5.15}"
LOCALSTACK_ENDPOINT="http://127.0.0.1:4566"
API_URL="http://127.0.0.1:8000"
WEB_URL="http://127.0.0.1:8080"
GRAFANA_URL="http://127.0.0.1:3000"
PROMETHEUS_URL="http://127.0.0.1:9090"
TERRAFORM_DIR="$REPO_ROOT/terraform/local"
export CLUSTER_NAME NAMESPACE RELEASE_NAME LOCALSTACK_NAMESPACE MONITORING_NAMESPACE
export LOCALSTACK_ENDPOINT API_URL WEB_URL GRAFANA_URL PROMETHEUS_URL TERRAFORM_DIR
export PROMETHEUS_CHART_VERSION GRAFANA_CHART_VERSION

if [ -t 1 ]; then
  C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_OFF=$'\033[0m'
else
  C_BLUE=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_OFF=''
fi

step() { printf '%s==>%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_YELLOW" "$C_OFF" "$*"; }
fail() { printf '%sERROR%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# kubectl scoped to the demo cluster, so a script can never act on whatever context happens to be
# selected in the user's shell.
kc() { kubectl --context "kind-${CLUSTER_NAME}" "$@"; }

cluster_exists() {
  have kind && kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"
}

# wait_for <timeout-seconds> <command...>
# Polls until the command succeeds, printing a dot per attempt. Callers print their own message
# first, so this deliberately takes no description.
wait_for() {
  local timeout="$1"; shift
  local deadline=$(( $(date +%s) + timeout ))
  while true; do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      printf '\n'
      return 1
    fi
    printf '.'
    sleep 2
  done
}
