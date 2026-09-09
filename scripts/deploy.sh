#!/usr/bin/env bash
# Rebuild the image and upgrade the release, without touching the cluster, LocalStack or
# Terraform. The fast inner loop for code changes.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

cluster_exists || fail "no kind cluster named '$CLUSTER_NAME'. Run: make up"

image_tag="$("$REPO_ROOT/scripts/build-images.sh" | tail -n 1)"
ok "image tag: $image_tag"

step "Upgrading the Helm release"
helm --kube-context "kind-${CLUSTER_NAME}" upgrade "$RELEASE_NAME" \
  "$REPO_ROOT/helm/convoscore" \
  --namespace "$NAMESPACE" \
  --reuse-values \
  --set image.tag="$image_tag" \
  --wait \
  --timeout 10m

ok "deployed"
"$REPO_ROOT/scripts/status.sh" --brief
