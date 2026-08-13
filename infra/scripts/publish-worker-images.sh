#!/usr/bin/env bash
# Build the worker base image chain and publish it to GHCR as one release keyed by a git SHA.
#
# The chain is not invented here: `make rebuild-worker-images` already builds common
# and then claude, codex and factory from that exact common, stamping every image
# with `--build-arg SOURCE_HASH`. This script is the publishing half that was missing —
# it verifies what that build produced, pushes it under one immutable tag per image,
# and records the digests it published.
#
# The release of a SHA is written once and never rewritten. Before anything is built
# or pushed, every tag of the chain for GIT_SHA is resolved in the registry:
#
#   nothing published  -> build, verify the source hash of each image, push all four
#   all four published -> this SHA is already released: re-verify it from its digests
#                         and record it, push nothing, succeed (a rerun is idempotent)
#   some published     -> refuse, naming which tags exist and which are missing
#
# That last state is a half-published SHA, which acceptance criterion 1 says must not
# be left behind: a push that fails mid-chain is a decision (delete the package
# versions, or release the next commit), not something a rerun should silently
# complete over. A legitimate rebuild of the same SHA fails here by design — the
# published digests are what the deploy verifies, so replacing them would break the
# meaning of the recorded release.
#
# Required env vars:
#   GHCR_TOKEN   — GitHub token with packages:write scope
#   GHCR_OWNER   — GitHub org/user that owns the package namespace
#   GIT_SHA      — the commit being published; it is also the image tag
#   DIGEST_FILE  — where to write the machine-readable record of the release
#
# Exit codes, one per reason so a caller can tell them apart:
#   1  usage: a required variable is missing
#   2  what was just built does not carry the source hash of this tree
#   6  this SHA is partially published: some tags exist and some do not
#   7  an already-published image of this SHA carries the wrong source hash
#   8  a tag that was just pushed does not resolve to a digest

set -euo pipefail

EXIT_BUILT_LABEL=2
EXIT_PARTIAL_RELEASE=6
EXIT_PUBLISHED_LABEL=7
EXIT_UNRESOLVED=8

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

echo "Logging in to GHCR..."
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_OWNER}" --password-stdin

# Does this SHA already have a release? Ask before building anything, so a rerun of a
# published commit costs one lookup per image and touches no tag.
published=()
missing=()
for image in "${WORKER_BASE_IMAGES[@]}"; do
    remote="${REGISTRY}/${image}:${GIT_SHA}"
    if digest="$(worker_image_digest "${remote}" 2>/dev/null)" && [ -n "${digest}" ]; then
        echo "  ${remote} is already published as ${digest}"
        published+=("${image}=${REGISTRY}/${image}@${digest}")
    else
        echo "  ${remote} is not published"
        missing+=("${image}")
    fi
done

if [ "${#missing[@]}" -eq 0 ]; then
    echo "${GIT_SHA} is already published as a whole release; nothing will be pushed."
    for record in "${published[@]}"; do
        image="${record%%=*}"
        reference="${record#*=}"
        docker pull "${reference}" >/dev/null
        found="$(docker inspect "${reference}" \
            --format "{{index .Config.Labels \"${WORKER_SOURCE_HASH_LABEL}\"}}")"
        if [ "${found}" != "${SOURCE_HASH}" ]; then
            echo "FATAL: the published ${image} of ${GIT_SHA} (${reference})" >&2
            echo "       carries ${WORKER_SOURCE_HASH_LABEL}=${found:-(no label)}," >&2
            echo "       the tree is ${SOURCE_HASH}. The published tag is not rewritten." >&2
            exit "${EXIT_PUBLISHED_LABEL}"
        fi
        echo "  ${image}: ${WORKER_SOURCE_HASH_LABEL}=${found}"
    done
    worker_image_record "${GIT_SHA}" "${SOURCE_HASH}" "${DIGEST_FILE}" "${published[@]}"
    exit 0
fi

if [ "${#published[@]}" -ne 0 ]; then
    echo "FATAL: ${GIT_SHA} is half published, so this release cannot be completed here." >&2
    echo "       already in the registry:" >&2
    for record in "${published[@]}"; do
        echo "         ${record#*=}" >&2
    done
    echo "       missing:" >&2
    for image in "${missing[@]}"; do
        echo "         ${REGISTRY}/${image}:${GIT_SHA}" >&2
    done
    echo "       A published tag is never overwritten. Delete the package versions listed" >&2
    echo "       above and rerun, or publish the next commit." >&2
    exit "${EXIT_PARTIAL_RELEASE}"
fi

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
    echo "Pushing ${remote}..."
    docker tag "${image}:latest" "${remote}"
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
