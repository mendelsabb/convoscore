#!/usr/bin/env bash
# Check everything `make up` needs, and say exactly how to fix what is missing.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

missing=0

require() {
  local tool="$1" hint="$2"
  if have "$tool"; then
    ok "$tool"
  else
    printf '%s   no%s %-10s %s\n' "$C_RED" "$C_OFF" "$tool" "$hint"
    missing=1
  fi
}

step "Checking prerequisites"
require docker    "install Docker Desktop: https://docs.docker.com/desktop/"
require kind      "brew install kind"
require kubectl   "brew install kubectl"
require helm      "brew install helm"
require terraform "brew install terraform"

if have docker; then
  if docker info >/dev/null 2>&1; then
    ok "docker daemon is running"
  else
    printf '%s   no%s docker daemon is not running. Start Docker Desktop and try again.\n' "$C_RED" "$C_OFF"
    missing=1
  fi
fi

[ "$missing" -eq 0 ] || fail "missing prerequisites (see above)"

step "Checking configuration"
if [ -f "$REPO_ROOT/.env" ]; then
  # The file is only inspected for the presence of a key, never printed.
  if grep -qE '^[[:space:]]*OPENAI_API_KEY=..*' "$REPO_ROOT/.env" \
     && ! grep -qE '^[[:space:]]*OPENAI_API_KEY=sk-replace-me[[:space:]]*$' "$REPO_ROOT/.env"; then
    ok ".env contains an OpenAI API key"
  elif grep -qE '^[[:space:]]*LLM_PROVIDER=fake' "$REPO_ROOT/.env"; then
    ok ".env selects the fake provider (no OpenAI spend)"
  else
    warn ".env has no usable OPENAI_API_KEY."
    warn "Add your key, or set LLM_PROVIDER=fake to run the whole system without OpenAI."
    fail "no scoring backend configured"
  fi
else
  warn "no .env file found."
  warn "Run: cp .env.example .env   then add your OpenAI key (the file is gitignored)."
  fail ".env is required"
fi

# Docker Desktop's default allocation is enough, but a very small VM will fail in confusing ways
# once PostgreSQL, LocalStack and the application are all running.
if have docker && docker info >/dev/null 2>&1; then
  total_bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
  if [ "${total_bytes:-0}" -gt 0 ]; then
    total_gb=$(( total_bytes / 1073741824 ))
    if [ "$total_gb" -lt 4 ]; then
      warn "Docker has ${total_gb}GB of memory. The stack needs roughly 3GB; 6GB is comfortable."
    else
      ok "docker memory: ${total_gb}GB"
    fi
  fi
fi

step "Prerequisites satisfied"
