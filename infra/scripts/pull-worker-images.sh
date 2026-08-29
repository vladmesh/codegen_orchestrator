#!/usr/bin/env bash
# Pull the worker base image release of one exact revision, and refuse anything else.
#
# The worker base images are one release chain keyed by the git SHA they were built
# from; the publishing half is `publish-worker-images.sh`, run by the
# publish-worker-images job of .github/workflows/ci.yml. This is the consuming half,
# and it is the one place that decides whether a revision has a release at all.
#
# It asks the release marker first (infra/scripts/worker-images.sh). Image tags do not
# make a release: four tag pushes cannot be one registry transaction, so a publish run
# that was cancelled or failed mid-chain leaves tags behind that were never a release.
# The marker is written last and only once all four images resolve, and it carries the
# digest record of them. No marker for this revision means there is no release for it,
# however many tags exist, and this refuses before a single local tag moves.
#
# What it then pulls, verifies, retags and records are the `<repository>@sha256:...`
# references the marker names — never a tag it resolves itself. One resolution is what
# makes the record provably the image that was verified. Nothing local moves before
# every image has been verified, so a refusal leaves the stand exactly as it was.
#
# There is no default tag and no fallback to a mutable :latest in the registry. A
# missing release for the revision being deployed is the failure this script exists to
# surface: on 2026-08-13 the mutable tag put worker images carrying an old
# WorkerWrapperConfig onto a green deploy of an exact SHA and every dynamic worker
# died before its agent started (GitHub #278).
#
# Required env vars:
#   GHCR_TOKEN        — GitHub token with packages:read scope
#   GHCR_OWNER        — GitHub org/user (e.g. "project-factory-organization")
#   WORKER_IMAGE_TAG  — the exact published tag to pull (the deployed git SHA)
#   DIGEST_FILE       — where to write the record of the digests that were verified
#
# Exit codes, one per reason so a caller can tell them apart:
#   1  usage: a required variable is missing, or the tag names a mutable image
#   3  the release names an image that is not in the registry
#   4  a pulled image carries no source hash label
#   5  a pulled image was built from a different source hash than this revision
#   6  the release marker exists but does not carry a usable record
#   9  this revision has no release marker: it was never published as a whole
#  10  registry authentication or authorization failed
#  11  registry transport or DNS failed
#  12  registry rate limiting failed
#  13  image-resolution tooling failed or returned an unclassified error

set -euo pipefail

EXIT_USAGE=1
EXIT_MISSING_IMAGE=3
EXIT_MISSING_LABEL=4
EXIT_STALE_LABEL=5
EXIT_BROKEN_RELEASE=6
EXIT_NO_RELEASE=9
EXIT_REGISTRY_AUTH=10
EXIT_REGISTRY_TRANSPORT=11
EXIT_REGISTRY_RATE_LIMIT=12
EXIT_REGISTRY_TOOL=13

: "${GHCR_TOKEN:?GHCR_TOKEN is required}"
: "${GHCR_OWNER:?GHCR_OWNER is required}"
: "${WORKER_IMAGE_TAG:?WORKER_IMAGE_TAG is required: the exact tag published for the revision being deployed, and there is no default}"
: "${DIGEST_FILE:?DIGEST_FILE is required: where to write the digests that were verified}"

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
MARKER="${REGISTRY}/${WORKER_RELEASE_MARKER_IMAGE}:${WORKER_IMAGE_TAG}"

echo "Logging in to GHCR..."
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_OWNER}" --password-stdin

echo "Deployed revision carries ${WORKER_SOURCE_HASH_LABEL}=${EXPECTED_HASH}"

# Is there a release for this revision at all? Nothing else is consulted, and nothing
# local moves until this answers yes.
if marker_resolution="$(worker_image_digest "${MARKER}" 2>&1)"; then
    marker_digest="${marker_resolution}"
else
    # A nonzero inspect is not itself evidence that a release is absent. Only
    # the registry's manifest-unknown response means local building is safe;
    # credentials, transport, rate limiting and broken tooling must stay red.
    case "${marker_resolution,,}" in
        *"manifest unknown"*|*"manifest not found"*|*"404 not found"*)
            exit_code="${EXIT_NO_RELEASE}"
            ;;
        *"unauthorized"*|*"authentication required"*|*"denied: requested access"*)
            exit_code="${EXIT_REGISTRY_AUTH}"
            ;;
        *"too many requests"*|*"rate limit"*|*"429"*)
            exit_code="${EXIT_REGISTRY_RATE_LIMIT}"
            ;;
        *"no such host"*|*"temporary failure"*|*"connection refused"*|*"network is unreachable"*|*"i/o timeout"*|*"tls handshake timeout"*)
            exit_code="${EXIT_REGISTRY_TRANSPORT}"
            ;;
        *)
            exit_code="${EXIT_REGISTRY_TOOL}"
            ;;
    esac
    echo "FATAL: could not resolve ${MARKER}: ${marker_resolution}" >&2
    exit "${exit_code}"
fi
if [ -z "${marker_digest}" ]; then
    echo "FATAL: resolving ${MARKER} returned no digest; the registry response is unusable." >&2
    exit "${EXIT_REGISTRY_TOOL}"
fi

marker_reference="${REGISTRY}/${WORKER_RELEASE_MARKER_IMAGE}@${marker_digest}"
echo "Reading the release of ${WORKER_IMAGE_TAG} from ${marker_reference}..."
if ! docker pull "${marker_reference}" >/dev/null; then
    echo "FATAL: ${MARKER} resolves to ${marker_digest} but cannot be pulled," >&2
    echo "       so the release of ${WORKER_IMAGE_TAG} cannot be read." >&2
    exit "${EXIT_BROKEN_RELEASE}"
fi
payload="$(docker inspect "${marker_reference}" \
    --format "{{index .Config.Labels \"${WORKER_RELEASE_LABEL}\"}}")"
if [ -z "${payload}" ] || [ "${payload}" = "<no value>" ]; then
    echo "FATAL: ${marker_reference} carries no ${WORKER_RELEASE_LABEL} label," >&2
    echo "       so it does not say which images the release of ${WORKER_IMAGE_TAG} is." >&2
    exit "${EXIT_BROKEN_RELEASE}"
fi
if ! released="$(worker_release_images "${payload}" "${WORKER_IMAGE_TAG}" "${REGISTRY}")"; then
    echo "FATAL: the release marker of ${WORKER_IMAGE_TAG} (${marker_reference})" >&2
    echo "       does not name a deployable chain; see the reason above." >&2
    exit "${EXIT_BROKEN_RELEASE}"
fi

verified=()
while IFS= read -r record; do
    image="${record%%=*}"
    # From here on nothing resolves a tag: this digest is what gets pulled, verified,
    # retagged and recorded.
    reference="${record#*=}"

    echo "Pulling ${reference}..."
    if ! docker pull "${reference}"; then
        echo "FATAL: the release of ${WORKER_IMAGE_TAG} names ${reference}," >&2
        echo "       which is not in the registry. A committed release is missing one of" >&2
        echo "       its images; that is not repaired by deploying, and this revision" >&2
        echo "       cannot be deployed." >&2
        echo "       image:    ${image}" >&2
        exit "${EXIT_MISSING_IMAGE}"
    fi

    found="$(docker inspect "${reference}" \
        --format "{{index .Config.Labels \"${WORKER_SOURCE_HASH_LABEL}\"}}")"
    if [ -z "${found}" ] || [ "${found}" = "<no value>" ]; then
        echo "FATAL: ${reference} carries no ${WORKER_SOURCE_HASH_LABEL} label," >&2
        echo "       so it cannot say which sources it was built from." >&2
        echo "       image:    ${image}" >&2
        echo "       expected: ${EXPECTED_HASH}" >&2
        echo "       found:    (no label)" >&2
        exit "${EXIT_MISSING_LABEL}"
    fi
    if [ "${found}" != "${EXPECTED_HASH}" ]; then
        echo "FATAL: ${reference} was built from other sources than the deployed revision." >&2
        echo "       image:    ${image}" >&2
        echo "       expected: ${EXPECTED_HASH}" >&2
        echo "       found:    ${found}" >&2
        exit "${EXIT_STALE_LABEL}"
    fi

    echo "  ${reference}: ${WORKER_SOURCE_HASH_LABEL}=${found} matches the deployed revision"
    verified+=("${record}")
done <<< "${released}"

# Every image is verified; only now do the names worker-manager resolves move.
for record in "${verified[@]}"; do
    image="${record%%=*}"
    reference="${record#*=}"
    echo "Retagging ${reference} to ${image}:latest..."
    docker tag "${reference}" "${image}:latest"
done

# The record of what was deployed, written from the same digests that were verified.
worker_image_record "${WORKER_IMAGE_TAG}" "${EXPECTED_HASH}" "${DIGEST_FILE}" "${verified[@]}"

echo "Worker images ready (${WORKER_IMAGE_TAG}, source hash ${EXPECTED_HASH}):"
for image in "${WORKER_BASE_IMAGES[@]}"; do
    docker images --format "  {{.Repository}}:{{.Tag}} ({{.Size}})" "${image}:latest"
done
