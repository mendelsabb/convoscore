#!/usr/bin/env bash
# Bring the whole system up. Safe to run repeatedly: every step is a no-op if already done.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

started_at=$(date +%s)

"$REPO_ROOT/scripts/preflight.sh"

# --- 1. cluster ---------------------------------------------------------------------------
if cluster_exists; then
  step "kind cluster '$CLUSTER_NAME' already exists"
else
  step "Creating kind cluster '$CLUSTER_NAME'"
  kind create cluster --name "$CLUSTER_NAME" --config "$REPO_ROOT/platform/kind-config.yaml" --wait 120s
fi
kc cluster-info >/dev/null || fail "cannot reach the cluster"
ok "cluster ready"

# --- 2. LocalStack ------------------------------------------------------------------------
step "Deploying LocalStack (S3, SQS, IAM)"
# Pre-pull on the host and load into the node: the image is large, and doing it this way means a
# cluster rebuild reuses the local copy instead of downloading it again.
if ! docker image inspect localstack/localstack:4.14.0 >/dev/null 2>&1; then
  docker pull localstack/localstack:4.14.0
fi
kind load docker-image localstack/localstack:4.14.0 --name "$CLUSTER_NAME" >/dev/null 2>&1 || true
kc apply -f "$REPO_ROOT/platform/localstack.yaml" >/dev/null
kc -n "$LOCALSTACK_NAMESPACE" rollout status deploy/localstack --timeout=300s

printf '     waiting for the LocalStack endpoint'
wait_for 120 curl -sf "$LOCALSTACK_ENDPOINT/_localstack/health" \
  || fail "LocalStack did not become reachable on $LOCALSTACK_ENDPOINT"
printf '\n'
ok "LocalStack ready at $LOCALSTACK_ENDPOINT"

# --- 3. Terraform -------------------------------------------------------------------------
# The bucket, queue, dead-letter queue and IAM policies the application actually uses. LocalStack
# does not persist state, so this is re-applied on every `make up`; Terraform makes that a no-op
# when nothing has changed.
step "Provisioning AWS resources with Terraform"
terraform -chdir="$TERRAFORM_DIR" init -input=false -no-color >/dev/null
terraform -chdir="$TERRAFORM_DIR" apply -auto-approve -input=false -no-color >/dev/null
queue_name="$(terraform -chdir="$TERRAFORM_DIR" output -raw queue_name)"
bucket_name="$(terraform -chdir="$TERRAFORM_DIR" output -raw bucket_name)"
incoming_prefix="$(terraform -chdir="$TERRAFORM_DIR" output -raw incoming_prefix)"
ok "queue: $queue_name"
ok "bucket: $bucket_name (prefix $incoming_prefix)"

# --- 4. monitoring --------------------------------------------------------------------------
# Installed before the application so that the very first pods are scraped from the moment they
# start, rather than appearing in the graphs a minute late.
if [ "${SKIP_MONITORING:-0}" = "1" ]; then
  warn "skipping monitoring (SKIP_MONITORING=1)"
else
  step "Installing Prometheus and Grafana"
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update prometheus-community grafana >/dev/null

  helm --kube-context "kind-${CLUSTER_NAME}" upgrade --install prometheus \
    prometheus-community/prometheus \
    --namespace "$MONITORING_NAMESPACE" --create-namespace \
    --version "$PROMETHEUS_CHART_VERSION" \
    --values "$REPO_ROOT/platform/prometheus-values.yaml" \
    --wait --timeout 8m >/dev/null
  ok "prometheus"

  helm --kube-context "kind-${CLUSTER_NAME}" upgrade --install grafana \
    grafana/grafana \
    --namespace "$MONITORING_NAMESPACE" --create-namespace \
    --version "$GRAFANA_CHART_VERSION" \
    --values "$REPO_ROOT/platform/grafana-values.yaml" \
    --wait --timeout 8m >/dev/null
  ok "grafana"
fi

# --- 5. image -----------------------------------------------------------------------------
image_tag="$("$REPO_ROOT/scripts/build-images.sh" | tail -n 1)"
ok "image tag: $image_tag"

# --- 6. secrets ---------------------------------------------------------------------------
"$REPO_ROOT/scripts/secrets.sh"

# --- 7. application -----------------------------------------------------------------------
step "Installing the ConvoScore Helm release"
# The resource names come straight from terraform output, so the application is wired to exactly
# what Terraform created rather than to a name repeated in two places.
helm --kube-context "kind-${CLUSTER_NAME}" upgrade --install "$RELEASE_NAME" \
  "$REPO_ROOT/helm/convoscore" \
  --namespace "$NAMESPACE" \
  --set image.tag="$image_tag" \
  --set aws.queueName="$queue_name" \
  --set aws.bucket="$bucket_name" \
  --set aws.prefix="$incoming_prefix" \
  ${LLM_PROVIDER:+--set llm.provider="$LLM_PROVIDER"} \
  --wait \
  --timeout 10m

# --- 8. ready -----------------------------------------------------------------------------
printf '     waiting for the API'
wait_for 120 curl -sf "$API_URL/readyz" || warn "the API is not answering yet; see: make status"
printf '\n'

elapsed=$(( $(date +%s) - started_at ))
printf '\n'
step "ConvoScore is up (${elapsed}s)"
"$REPO_ROOT/scripts/status.sh" --brief
