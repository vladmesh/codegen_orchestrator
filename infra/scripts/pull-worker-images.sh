#!/usr/bin/env bash
# Pull worker base images from GHCR and retag to local names
# expected by worker-manager (worker-base-common:latest, worker-base-claude:latest, etc.)
#
# Required env vars:
#   GHCR_TOKEN  — GitHub token with packages:read scope
#   GHCR_OWNER  — GitHub org/user (e.g. "project-factory-organization")
#
# Optional:
#   WORKER_IMAGE_TAG — image tag to pull (default: "latest")

set -euo pipefail

: "${GHCR_TOKEN:?GHCR_TOKEN is required}"
: "${GHCR_OWNER:?GHCR_OWNER is required}"

TAG="${WORKER_IMAGE_TAG:-latest}"
REGISTRY="ghcr.io/${GHCR_OWNER}/codegen-orchestrator"

IMAGES=(
    "worker-base-common"
    "worker-base-claude"
    "worker-base-factory"
    "worker-base-codex"
)

echo "Logging in to GHCR..."
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_OWNER}" --password-stdin

for image in "${IMAGES[@]}"; do
    remote="${REGISTRY}/${image}:${TAG}"
    local="${image}:latest"

    echo "Pulling ${remote}..."
    docker pull "${remote}"

    echo "Retagging to ${local}..."
    docker tag "${remote}" "${local}"
done

echo "Cleaning cached worker:* images..."
docker images -q 'worker:*' | xargs -r docker rmi 2>/dev/null || true

echo "Worker images ready:"
for image in "${IMAGES[@]}"; do
    docker images --format "  {{.Repository}}:{{.Tag}} ({{.Size}})" "${image}:latest"
done
