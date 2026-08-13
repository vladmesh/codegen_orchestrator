#!/usr/bin/env bash
# Build the worker base image chain and publish it to GHCR as one release keyed by a git SHA.
#
# The chain is not invented here: `make rebuild-worker-images` already builds common
# and then claude, codex and factory from that exact common, stamping every image
# with `--build-arg SOURCE_HASH`. This script is the publishing half that was missing —
# it verifies what that build produced, pushes it under one immutable tag per image,
# and records the digests it published.
#
# One release chain: every image is verified before anything is pushed, the run fails
# on the first push that does not land, and the recorded digests are read back out of
# the registry afterwards, so a SHA is either published whole or the job is red. The
# consuming half refuses a half-published SHA on its side too — pull-worker-images.sh
# verifies all four images before it moves a single local tag.
#
# Required env vars:
#   GHCR_TOKEN   — GitHub token with packages:write scope
#   GHCR_OWNER   — GitHub org/user that owns the package namespace
#   GIT_SHA      — the commit being published; it is also the image tag
#   DIGEST_FILE  — where to write the machine-readable record of the release

set -euo pipefail

: "${GHCR_TOKEN:?GHCR_TOKEN is required}"
: "${GHCR_OWNER:?GHCR_OWNER is required}"
: "${GIT_SHA:?GIT_SHA is required: the commit being published, which is also the tag}"
: "${DIGEST_FILE:?DIGEST_FILE is required: where to write the published digests}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=infra/scripts/worker-images.sh
source "${SCRIPT_DIR}/worker-images.sh"

SOURCE_HASH="$(python3 "${REPO_ROOT}/scripts/shared_freshness.py" hash)"
REGISTRY="$(worker_image_registry "${GHCR_OWNER}")"

echo "Building the worker chain for ${GIT_SHA} (source hash ${SOURCE_HASH})..."
make -C "${REPO_ROOT}" rebuild-worker-images

# What was built has to say what it was built from, before any of it is published.
for image in "${WORKER_BASE_IMAGES[@]}"; do
    found="$(docker inspect "${image}:latest" \
        --format "{{index .Config.Labels \"${WORKER_SOURCE_HASH_LABEL}\"}}")"
    if [ "${found}" != "${SOURCE_HASH}" ]; then
        echo "FATAL: ${image}:latest carries ${WORKER_SOURCE_HASH_LABEL}=${found:-(no label)}," >&2
        echo "       the tree is ${SOURCE_HASH}. Nothing is published." >&2
        exit 1
    fi
    echo "  ${image}: ${WORKER_SOURCE_HASH_LABEL}=${found}"
done

echo "Logging in to GHCR..."
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_OWNER}" --password-stdin

for image in "${WORKER_BASE_IMAGES[@]}"; do
    remote="${REGISTRY}/${image}:${GIT_SHA}"
    echo "Pushing ${remote}..."
    docker tag "${image}:latest" "${remote}"
    docker push "${remote}"
done

echo "Recording the published release..."
GHCR_TOKEN="${GHCR_TOKEN}" \
GHCR_OWNER="${GHCR_OWNER}" \
GIT_SHA="${GIT_SHA}" \
SOURCE_HASH="${SOURCE_HASH}" \
DIGEST_FILE="${DIGEST_FILE}" \
    bash "${SCRIPT_DIR}/record-worker-image-digests.sh"
