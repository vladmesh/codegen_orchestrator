#!/usr/bin/env bash
# Build the worker base image chain and publish it to GHCR as one release keyed by a git SHA.
#
# The chain is not invented here: `make rebuild-worker-images` already builds common
# and then claude, codex and factory from that exact common, stamping every image
# with `--build-arg SOURCE_HASH`. This script is the publishing half that was missing —
# it verifies what that build produced, pushes it under one immutable tag per image,
# and then commits the release.
#
# Four tag pushes are not one registry transaction, and no shell can make them one.
# So a pushed tag is not a release here: the release of a SHA is one further object,
# the release marker (infra/scripts/worker-images.sh), written last and carrying the
# digest record of the four images. The puller resolves that marker first and deploys
# only the digests it names, so a run that dies mid-chain leaves bytes in the registry
# that nothing will ever act on.
#
# Which makes the marker, and only the marker, the state this script branches on:
#
#   marker resolves  -> this SHA is released and frozen. Re-verify the digests it
#                       names, rewrite the record, push nothing, succeed.
#   marker absent    -> this SHA is not released, however many image tags exist.
#                       Build, verify, push all four, then publish the marker. A run
#                       cancelled or failed mid-chain is recovered by rerunning this
#                       job, with nobody deleting anything in the registry.
#
# A marker that resolves but names an image that does not, or one built from other
# sources, is corruption of a committed release: it is refused and never repaired,
# because repairing it would change what an already-deployed release means.
#
# Required env vars:
#   GHCR_TOKEN   — GitHub token with packages:write scope
#   GHCR_OWNER   — GitHub org/user that owns the package namespace
#   GIT_SHA      — the commit being published; it is also the image tag
#   DIGEST_FILE  — where to write the machine-readable record of the release
#
# Exit codes, one per reason so a caller can tell them apart:
#   1   usage: a required variable is missing
#   2   what was just built does not carry the source hash of this tree
#   7   an image of an already-released SHA carries the wrong source hash
#   8   a tag that was just pushed does not resolve to a digest
#   10  the release marker of this SHA is unreadable or names an image that is gone

set -euo pipefail

EXIT_BUILT_LABEL=2
EXIT_RELEASED_LABEL=7
EXIT_UNRESOLVED=8
EXIT_BROKEN_RELEASE=10

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
MARKER="${REGISTRY}/${WORKER_RELEASE_MARKER_IMAGE}:${GIT_SHA}"

echo "Logging in to GHCR..."
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_OWNER}" --password-stdin

# Is this SHA released? Ask the marker, and only the marker, before building anything.
if marker_digest="$(worker_image_digest "${MARKER}" 2>/dev/null)" && [ -n "${marker_digest}" ]; then
    marker_reference="${REGISTRY}/${WORKER_RELEASE_MARKER_IMAGE}@${marker_digest}"
    echo "${GIT_SHA} is already released (${marker_reference}); nothing will be pushed."

    if ! docker pull "${marker_reference}" >/dev/null; then
        echo "FATAL: the release marker of ${GIT_SHA} resolves to ${marker_digest}" >&2
        echo "       but cannot be pulled, so the release cannot be re-verified." >&2
        exit "${EXIT_BROKEN_RELEASE}"
    fi
    payload="$(docker inspect "${marker_reference}" \
        --format "{{index .Config.Labels \"${WORKER_RELEASE_LABEL}\"}}")"
    if ! released="$(worker_release_images "${payload}" "${GIT_SHA}" "${REGISTRY}")"; then
        echo "FATAL: the release marker of ${GIT_SHA} does not carry a usable record." >&2
        exit "${EXIT_BROKEN_RELEASE}"
    fi

    records=()
    while IFS= read -r record; do
        image="${record%%=*}"
        reference="${record#*=}"
        if ! docker pull "${reference}" >/dev/null; then
            echo "FATAL: the release of ${GIT_SHA} names ${reference}," >&2
            echo "       which is not in the registry. A committed release is not repaired" >&2
            echo "       here: publish the next commit instead." >&2
            exit "${EXIT_BROKEN_RELEASE}"
        fi
        found="$(docker inspect "${reference}" \
            --format "{{index .Config.Labels \"${WORKER_SOURCE_HASH_LABEL}\"}}")"
        if [ "${found}" != "${SOURCE_HASH}" ]; then
            echo "FATAL: the released ${image} of ${GIT_SHA} (${reference})" >&2
            echo "       carries ${WORKER_SOURCE_HASH_LABEL}=${found:-(no label)}," >&2
            echo "       the tree is ${SOURCE_HASH}. A released SHA is never rewritten." >&2
            exit "${EXIT_RELEASED_LABEL}"
        fi
        echo "  ${image}: ${WORKER_SOURCE_HASH_LABEL}=${found}"
        records+=("${record}")
    done <<< "${released}"

    worker_image_record "${GIT_SHA}" "${SOURCE_HASH}" "${DIGEST_FILE}" "${records[@]}"
    exit 0
fi

# No marker: this SHA is not released. Anything of it already in the registry is
# residue of a run that did not finish, and pushing over it releases nothing by itself.
echo "${GIT_SHA} has no release marker; building and publishing it."
echo "Building the worker chain for ${GIT_SHA} (source hash ${SOURCE_HASH})..."
make -C "${REPO_ROOT}" rebuild-worker-images

# What was built has to say what it was built from, before any of it is published.
for image in "${WORKER_BASE_IMAGES[@]}"; do
    found="$(docker inspect "${image}:latest" \
        --format "{{index .Config.Labels \"${WORKER_SOURCE_HASH_LABEL}\"}}")"
    if [ "${found}" != "${SOURCE_HASH}" ]; then
        echo "FATAL: ${image}:latest carries ${WORKER_SOURCE_HASH_LABEL}=${found:-(no label)}," >&2
        echo "       the tree is ${SOURCE_HASH}. Nothing is published." >&2
        exit "${EXIT_BUILT_LABEL}"
    fi
    echo "  ${image}: ${WORKER_SOURCE_HASH_LABEL}=${found}"
done

for image in "${WORKER_BASE_IMAGES[@]}"; do
    remote="${REGISTRY}/${image}:${GIT_SHA}"
    docker tag "${image}:latest" "${remote}"
    echo "Pushing ${remote}..."
    docker push "${remote}"
done

echo "Recording the published release..."
records=()
for image in "${WORKER_BASE_IMAGES[@]}"; do
    remote="${REGISTRY}/${image}:${GIT_SHA}"
    digest="$(worker_image_digest "${remote}")"
    if [ -z "${digest}" ]; then
        echo "FATAL: ${remote} has no digest in the registry after being pushed." >&2
        exit "${EXIT_UNRESOLVED}"
    fi
    echo "  ${remote} -> ${digest}"
    records+=("${image}=${REGISTRY}/${image}@${digest}")
done
worker_image_record "${GIT_SHA}" "${SOURCE_HASH}" "${DIGEST_FILE}" "${records[@]}"

# Every image of the chain resolves and the record of it is written. This last write
# is the release: before it, nothing may deploy this SHA; after it, nothing may
# change it.
echo "Publishing the release marker ${MARKER}..."
worker_release_marker_publish "${MARKER}" "${DIGEST_FILE}"
echo "${GIT_SHA} is released."
