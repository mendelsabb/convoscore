#!/usr/bin/env bash
# Turn the gitignored .env into Kubernetes Secrets.
#
# Secrets exist only here and in the cluster. They are never written into Helm values, never baked
# into an image, and never printed. The Helm chart refers to them by name only, so `helm template`
# output is safe to paste anywhere.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

step "Creating namespace and secrets"

kc create namespace "$NAMESPACE" --dry-run=client -o yaml | kc apply -f - >/dev/null
ok "namespace $NAMESPACE"

# --- OpenAI key -------------------------------------------------------------------------
# Read without echoing. Only the presence and emptiness of the value are ever reported.
openai_key=""
if [ -f "$REPO_ROOT/.env" ]; then
  openai_key="$(
    grep -E '^[[:space:]]*OPENAI_API_KEY=' "$REPO_ROOT/.env" 2>/dev/null \
      | tail -n 1 \
      | sed -E 's/^[[:space:]]*OPENAI_API_KEY=//' \
      | sed -E 's/^["'"'"']//; s/["'"'"']$//' \
      | tr -d '\r' || true
  )"
fi

if [ -z "$openai_key" ] || [ "$openai_key" = "sk-replace-me" ]; then
  # The chart only mounts this secret when the provider is openai, but the object must exist for
  # the manifest to be valid either way.
  openai_key="unset"
  warn "no OpenAI key found; creating a placeholder secret (use LLM_PROVIDER=fake to run without one)"
fi

kc -n "$NAMESPACE" create secret generic convoscore-openai \
  --from-literal=OPENAI_API_KEY="$openai_key" \
  --dry-run=client -o yaml | kc apply -f - >/dev/null
unset openai_key
ok "secret convoscore-openai"

# --- PostgreSQL password ------------------------------------------------------------------
# Generated once and then left alone. Regenerating it on every `make up` would lock the release
# out of an existing PersistentVolumeClaim, because the database keeps the password it was
# initialised with.
if kc -n "$NAMESPACE" get secret convoscore-postgres >/dev/null 2>&1; then
  ok "secret convoscore-postgres (existing password kept)"
else
  # Note: no `... /dev/urandom | head -c 32`. head closes the pipe early, the upstream process
  # takes SIGPIPE, and under `set -o pipefail` that aborts the whole script.
  if have openssl; then
    password="$(openssl rand -hex 24)"
  else
    password="$(head -c 4096 /dev/urandom | LC_ALL=C tr -dc 'a-zA-Z0-9' | cut -c1-32)"
  fi
  kc -n "$NAMESPACE" create secret generic convoscore-postgres \
    --from-literal=POSTGRES_PASSWORD="$password" >/dev/null
  unset password
  ok "secret convoscore-postgres (new password generated)"
fi
