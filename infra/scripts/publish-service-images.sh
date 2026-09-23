#!/usr/bin/env bash
# Publish the control-plane service images to GHCR as one release keyed by a git SHA.
#
# The protocol is the worker chain's (publish-worker-images.sh, release-chain.sh): image
# tags are candidates, and the release of a SHA is one further object, the release
# marker, written last and carrying the digest record of every image. What differs is
# when the two halves run, so they are two stages of this script:
#
#   candidates  build every image of service-images.sh once with buildx and push it
#               under the SHA tag. Runs beside the test jobs, before anything is known
#               to be green, which is safe only because a candidate is not a release.
#   release     runs only after the Required CI Gate is green (ci.yml). Resolves every
#               candidate tag once, checks the source hash label of that digest, and
#               then writes the marker. This stage is the only writer of the marker.
#
# The marker, and only the marker, is the state both stages branch on:
#
#   marker resolves  -> this SHA is released and frozen. Both stages re-verify the
#                       release the marker names (release_verify_committed) and push
#                       nothing; they succeed only if it verifies, and `release` then
#                       rewrites the record.
#   marker absent    -> this SHA is not released, however many candidate tags exist.
#                       `candidates` builds and pushes over them; `release` verifies
#                       them and commits the release. A run that died in between is
#                       recovered by rerunning, with nobody deleting anything.
#   lookup fails     -> the registry cannot say (credentials, transport, 5xx): the SHA
#                       may be released, so neither stage pushes anything (exit 11).
#                       Only a registry 404 on the marker's manifest means absent
#                       (release_marker_lookup in release-chain.sh).
#
# A marker that resolves but is unreadable, is not a valid record of this release
# (another SHA, source hash or schema, another set of images, a reference its own
# repository/digest fields contradict), or names an image that is gone or carries
# another source hash, is corruption of a committed release: refused, never repaired.
#
# Usage: publish-service-images.sh candidates|release
#
# Required env vars:
#   GHCR_TOKEN   — GitHub token with packages:write scope
#   GHCR_OWNER   — GitHub org/user that owns the package namespace
#   GIT_SHA      — the commit being published; it is also the image tag
#   DIGEST_FILE  — release only: where to write the machine-readable record
#
# Exit codes, one per reason so a caller can tell them apart:
#   1   usage: no stage, an unknown stage, a missing variable, or an empty tree hash
#   2   a candidate carries the source hash of another tree
#   3   a candidate carries no source hash label, or an empty one
#   4   building or pushing a candidate failed
#   7   an image of an already-released SHA carries a wrong or empty source hash
#   8   a candidate tag does not resolve to a digest, or that digest cannot be pulled
#   10  the release marker of this SHA is unreadable, is not a valid record of this
#       release, or names an image that is gone
#   11  the registry cannot say whether this SHA is released: nothing is pushed
#   12  building or pushing the release marker failed: the SHA is not released
# 7, 10, 11 and 12 are the release protocol's own codes (release-chain.sh), the same in
# every chain.

set -euo pipefail

EXIT_USAGE=1
EXIT_CANDIDATE_LABEL=2
EXIT_CANDIDATE_NO_LABEL=3
EXIT_BUILD=4
EXIT_UNRESOLVED=8

STAGE="${1:-}"
case "${STAGE}" in
    candidates | release) ;;
    *)
        echo "usage: $0 candidates|release" >&2
        exit "${EXIT_USAGE}"
        ;;
esac

require() {
    if [ -z "${!1:-}" ]; then
        echo "FATAL: $1 is required: $2" >&2
        exit "${EXIT_USAGE}"
    fi
}
require GHCR_TOKEN "a GitHub token with packages:write scope"
require GHCR_OWNER "the GitHub org/user that owns the package namespace"
require GIT_SHA "the commit being published, which is also the tag"
if [ "${STAGE}" = release ]; then
    require DIGEST_FILE "where to write the published digests"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=infra/scripts/service-images.sh
source "${SCRIPT_DIR}/service-images.sh"
EXIT_MARKER_LOOKUP="${RELEASE_EXIT_MARKER_LOOKUP}"

SOURCE_HASH="$(python3 "${REPO_ROOT}/scripts/shared_freshness.py" hash)"
if [ -z "${SOURCE_HASH}" ]; then
    echo "FATAL: scripts/shared_freshness.py hash printed nothing; nothing is published." >&2
    exit "${EXIT_USAGE}"
fi
REGISTRY="$(release_image_registry "${GHCR_OWNER}")"
MARKER="${REGISTRY}/${SERVICE_RELEASE_MARKER_IMAGE}:${GIT_SHA}"

echo "Logging in to GHCR..."
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_OWNER}" --password-stdin

# Is this SHA released? Ask the marker, and only the marker, before anything else.
# Only an "absent" answer gets past this point, so every push below — the candidates and
# the marker — is reached through it alone.
release_marker_lookup "${MARKER}" "${GHCR_OWNER}" "${GHCR_TOKEN}"
case "${RELEASE_MARKER_STATE}" in
    present)
        marker_reference="${REGISTRY}/${SERVICE_RELEASE_MARKER_IMAGE}@${RELEASE_MARKER_DIGEST}"
        echo "${GIT_SHA} is already released (${marker_reference}); nothing will be pushed."
        # Both stages run the same re-verification: the candidate tags of a released
        # SHA are the images its marker names, and a stage that succeeds here says they
        # still are this tree's release.
        # shellcheck disable=SC2046 # one word per image name
        released="$(release_verify_committed "${marker_reference}" "${SERVICE_RELEASE_LABEL}" \
            "${GIT_SHA}" "${SOURCE_HASH}" "${SERVICE_SOURCE_HASH_LABEL}" "${REGISTRY}" \
            "${SERVICE_RELEASE_SCHEMA_VERSION}" $(service_image_names))" || exit "$?"
        if [ "${STAGE}" = release ]; then
            mapfile -t records <<< "${released}"
            release_record "${GIT_SHA}" "${SOURCE_HASH}" "${DIGEST_FILE}" \
                "${SERVICE_RELEASE_SCHEMA_VERSION}" "${records[@]}"
        fi
        echo "The release of ${GIT_SHA} verifies."
        exit 0
        ;;
    absent) ;;
    *)
        echo "FATAL: the registry cannot say whether ${GIT_SHA} is released (${MARKER});" >&2
        echo "       see the reason above. Nothing is pushed: rerun once the registry answers." >&2
        exit "${EXIT_MARKER_LOOKUP}"
        ;;
esac

# No marker: this SHA is not released. Any candidate of it already in the registry is
# residue of a run that did not finish, and pushing over it releases nothing by itself.
if [ "${STAGE}" = candidates ]; then
    echo "${GIT_SHA} has no release marker; building its candidates (source hash ${SOURCE_HASH})."
    # One plain image manifest per tag (no provenance index), the shape the worker chain
    # pushes; the layer cache goes to its own repository, never next to a release image.
    for entry in "${SERVICE_IMAGES[@]}"; do
        read -r image dockerfile context <<< "${entry}"
        remote="${REGISTRY}/${image}:${GIT_SHA}"
        cache="${REGISTRY}/${SERVICE_BUILD_CACHE_IMAGE}:${image}"
        echo "Building and pushing ${remote}..."
        if ! (cd "${REPO_ROOT}" && docker buildx build \
            --file "${dockerfile}" \
            --build-arg "SOURCE_HASH=${SOURCE_HASH}" \
            --tag "${remote}" \
            --cache-from "type=registry,ref=${cache}" \
            --cache-to "type=registry,ref=${cache},mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true" \
            --provenance=false \
            --push \
            "${context}"); then
            echo "FATAL: building or pushing ${remote} failed. Its SHA has no release;" >&2
            echo "       rerun this job to push its candidates again." >&2
            exit "${EXIT_BUILD}"
        fi
    done
    echo "Every candidate of ${GIT_SHA} is pushed; the release job commits them once CI is green."
    exit 0
fi

echo "${GIT_SHA} has no release marker; verifying its candidates (source hash ${SOURCE_HASH})."
records=()
for image in $(service_image_names); do
    remote="${REGISTRY}/${image}:${GIT_SHA}"
    # The one resolution of this tag: everything below names this digest.
    if ! digest="$(release_image_digest "${remote}")" || [ -z "${digest}" ]; then
        echo "FATAL: ${remote} does not resolve to a digest: its candidate was never pushed." >&2
        echo "       Nothing is released; rerun the candidate build, then this job." >&2
        exit "${EXIT_UNRESOLVED}"
    fi
    reference="${REGISTRY}/${image}@${digest}"
    if ! docker pull "${reference}" >/dev/null; then
        echo "FATAL: ${remote} resolves to ${digest}, which cannot be pulled." >&2
        exit "${EXIT_UNRESOLVED}"
    fi
    if ! found="$(release_source_hash_of "${reference}" "${SERVICE_SOURCE_HASH_LABEL}")"; then
        echo "FATAL: ${reference} (${image}) was pulled but cannot be inspected." >&2
        exit "${EXIT_UNRESOLVED}"
    fi
    if [ -z "${found}" ]; then
        echo "FATAL: ${reference} carries no ${SERVICE_SOURCE_HASH_LABEL} label," >&2
        echo "       so it cannot say which sources it was built from. Nothing is released." >&2
        exit "${EXIT_CANDIDATE_NO_LABEL}"
    fi
    if [ "${found}" != "${SOURCE_HASH}" ]; then
        echo "FATAL: ${reference} (${image}) carries ${SERVICE_SOURCE_HASH_LABEL}=${found}," >&2
        echo "       the tree is ${SOURCE_HASH}. Nothing is released." >&2
        exit "${EXIT_CANDIDATE_LABEL}"
    fi
    echo "  ${remote} -> ${digest} (${SERVICE_SOURCE_HASH_LABEL}=${found})"
    records+=("${image}=${reference}")
done
release_record "${GIT_SHA}" "${SOURCE_HASH}" "${DIGEST_FILE}" \
    "${SERVICE_RELEASE_SCHEMA_VERSION}" "${records[@]}"

# Every image resolves to a digest built from this tree, and the record of them is
# written. This last write is the release: before it nothing may consume this SHA's
# service images; after it nothing may change them.
echo "Publishing the service release marker ${MARKER}..."
release_marker_publish "${SERVICE_RELEASE_LABEL}" service-images.json "${MARKER}" "${DIGEST_FILE}" \
    || exit "$?"
echo "${GIT_SHA} is released."
