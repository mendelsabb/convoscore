#!/usr/bin/env bash
# Build the application image and load it into the kind node.
#
# Prints the tag on stdout as the last line so up.sh can capture it. Everything else goes to
# stderr, which keeps the contract simple.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

exec 3>&1
log() { printf '%s\n' "$*" >&2; }

# A content-independent unique tag per build. A fixed tag such as "latest" would leave the old
# image in the node's store and Kubernetes would have no reason to restart the pods.
short_sha="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
tag="${IMAGE_TAG:-${short_sha}-$(date +%Y%m%d%H%M%S)}"
image="convoscore-backend:${tag}"

log "${C_BLUE}==>${C_OFF} Building ${image}"
docker build \
  --tag "$image" \
  --file "$REPO_ROOT/backend/Dockerfile" \
  "$REPO_ROOT/backend" >&2

log "${C_BLUE}==>${C_OFF} Loading ${image} into kind cluster ${CLUSTER_NAME}"
kind load docker-image "$image" --name "$CLUSTER_NAME" >&2

printf '%s\n' "$tag" >&3
