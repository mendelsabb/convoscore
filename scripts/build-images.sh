#!/usr/bin/env bash
# Build the application images and load them into the kind node.
#
# Prints the shared tag on stdout as the last line so up.sh can capture it. Everything else goes
# to stderr, which keeps the contract simple.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

exec 3>&1
log() { printf '%s\n' "$*" >&2; }

# One tag for both images, unique per build. A fixed tag such as "latest" would leave the old
# image in the node's store and Kubernetes would have no reason to restart the pods.
short_sha="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
tag="${IMAGE_TAG:-${short_sha}-$(date +%Y%m%d%H%M%S)}"

build_and_load() {
  local name="$1" context="$2"
  local image="${name}:${tag}"

  log "${C_BLUE}==>${C_OFF} Building ${image}"
  docker build --tag "$image" --file "${context}/Dockerfile" "$context" >&2

  log "${C_BLUE}==>${C_OFF} Loading ${image} into kind cluster ${CLUSTER_NAME}"
  kind load docker-image "$image" --name "$CLUSTER_NAME" >&2
}

build_and_load convoscore-backend "$REPO_ROOT/backend"
build_and_load convoscore-web "$REPO_ROOT/frontend"

printf '%s\n' "$tag" >&3
