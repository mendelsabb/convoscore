#!/usr/bin/env bash
# Run the automated test suite. No test ever calls OpenAI.
#
# Brings up a throwaway PostgreSQL (docker compose), applies the real migrations, runs pytest.
# Pass --keep to leave the database running for a faster next run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Absolute: the script changes directory before running pytest, and the cleanup trap must still
# be able to find this file.
COMPOSE_FILE="$REPO_ROOT/docker-compose.test.yaml"
KEEP_DB=0
# Consume --keep so it is not forwarded to pytest; anything else is passed straight through
# (for example: scripts/test.sh tests/unit -k claim).
if [[ "${1:-}" == "--keep" ]]; then
  KEEP_DB=1
  shift
fi

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "docker is required to run the test database"
docker info >/dev/null 2>&1 || fail "the Docker daemon is not running; start Docker Desktop first"
command -v uv >/dev/null 2>&1 || fail "uv is required (https://docs.astral.sh/uv/ or: brew install uv)"

cleanup() {
  if [[ "$KEEP_DB" -eq 0 ]]; then
    log "stopping the test database"
    docker compose -f "$COMPOSE_FILE" down --volumes --remove-orphans >/dev/null 2>&1 || true
  else
    log "leaving the test database running (--keep); stop it with: docker compose -f $COMPOSE_FILE down -v"
  fi
}
trap cleanup EXIT

log "starting PostgreSQL for tests"
docker compose -f "$COMPOSE_FILE" up -d --wait

log "running backend tests"
cd "$REPO_ROOT/backend"
uv run --extra dev pytest "$@"

log "all tests passed"
