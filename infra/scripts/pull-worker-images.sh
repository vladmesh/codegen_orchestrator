#!/usr/bin/env bash
# Pull the worker base image release of one exact revision, and refuse anything else.
#
# The worker base images are one release chain keyed by the git SHA they were built
# from; the publishing half is `publish-worker-images.sh`, run by the
# publish-worker-images job of .github/workflows/ci.yml. This is the consuming half.
#
# It pulls the exact tag it is told to pull, checks that every pulled image carries
# the source hash of the revision checked out here, and only then moves the local
# worker-base-*:latest names that worker-manager resolves
# (services/worker-manager/src/image_builder.py). Nothing local moves before every
# image has been verified, so a refusal leaves the stand exactly as it was.
#
# There is no default tag and no fallback to a mutable :latest in the registry. A
# missing image for the revision being deployed is the failure this script exists to
# surface: on 2026-08-13 the mutable tag put worker images carrying an old
# WorkerWrapperConfig onto a green deploy of an exact SHA and every dynamic worker
# died before its agent started (GitHub #278).
#
# Required env vars:
#   GHCR_TOKEN        — GitHub token with packages:read scope
#   GHCR_OWNER        — GitHub org/user (e.g. "project-factory-organization")
#   WORKER_IMAGE_TAG  — the exact published tag to pull (the deployed git SHA)
#
# Exit codes, one per reason so a caller can tell them apart:
#   1  usage: a required variable is missing, or the tag names a mutable image
#   3  the image does not exist in the registry under that tag
#   4  a pulled image carries no source hash label
#   5  a pulled image was built from a different source hash than this revision

set -euo pipefail

EXIT_USAGE=1
EXIT_MISSING_IMAGE=3
EXIT_MISSING_LABEL=4
EXIT_STALE_LABEL=5

: "${GHCR_TOKEN:?GHCR_TOKEN is required}"
: "${GHCR_OWNER:?GHCR_OWNER is required}"
: "${WORKER_IMAGE_TAG:?WORKER_IMAGE_TAG is required: the exact tag published for the revision being deployed, and there is no default}"

if [ "${WORKER_IMAGE_TAG}" = "latest" ]; then
    echo "FATAL: WORKER_IMAGE_TAG=latest names a mutable image, not a revision." >&2
    echo "       Pass the git SHA the deployment is running." >&2
    exit "${EXIT_USAGE}"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=infra/scripts/worker-images.sh
source "${SCRIPT_DIR}/worker-images.sh"

# The single producer of this value, the same one the Makefile and the publish half read.
EXPECTED_HASH="$(python3 "${REPO_ROOT}/scripts/shared_freshness.py" hash)"
REGISTRY="$(worker_image_registry "${GHCR_OWNER}")"

echo "Logging in to GHCR..."
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_OWNER}" --password-stdin

echo "Deployed revision carries ${WORKER_SOURCE_HASH_LABEL}=${EXPECTED_HASH}"

for image in "${WORKER_BASE_IMAGES[@]}"; do
    remote="${REGISTRY}/${image}:${WORKER_IMAGE_TAG}"

    echo "Pulling ${remote}..."
    if ! docker pull "${remote}"; then
        echo "FATAL: ${remote} is not published." >&2
        echo "       The worker base images for ${WORKER_IMAGE_TAG} are missing, so there is" >&2
        echo "       nothing to deploy with. Publish that revision before deploying it." >&2
        exit "${EXIT_MISSING_IMAGE}"
    fi

    found="$(docker inspect "${remote}" \
        --format "{{index .Config.Labels \"${WORKER_SOURCE_HASH_LABEL}\"}}")"
    if [ -z "${found}" ] || [ "${found}" = "<no value>" ]; then
        echo "FATAL: ${remote} carries no ${WORKER_SOURCE_HASH_LABEL} label," >&2
        echo "       so it cannot say which sources it was built from." >&2
        echo "       expected: ${EXPECTED_HASH}" >&2
        echo "       found:    (no label)" >&2
        exit "${EXIT_MISSING_LABEL}"
    fi
    if [ "${found}" != "${EXPECTED_HASH}" ]; then
        echo "FATAL: ${remote} was built from other sources than the deployed revision." >&2
        echo "       expected: ${EXPECTED_HASH}" >&2
        echo "       found:    ${found}" >&2
        exit "${EXIT_STALE_LABEL}"
    fi

    echo "  ${remote}: ${WORKER_SOURCE_HASH_LABEL}=${found} matches the deployed revision"
done

# Every image is verified; only now do the names worker-manager resolves move.
for image in "${WORKER_BASE_IMAGES[@]}"; do
    remote="${REGISTRY}/${image}:${WORKER_IMAGE_TAG}"
    echo "Retagging ${remote} to ${image}:latest..."
    docker tag "${remote}" "${image}:latest"
done

echo "Worker images ready (${WORKER_IMAGE_TAG}, source hash ${EXPECTED_HASH}):"
for image in "${WORKER_BASE_IMAGES[@]}"; do
    docker images --format "  {{.Repository}}:{{.Tag}} ({{.Size}})" "${image}:latest"
done
