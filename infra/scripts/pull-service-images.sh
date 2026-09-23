#!/usr/bin/env bash
# Pull the control-plane service image release of one exact revision, and refuse anything
# else.
#
# The consuming half of the service image release chain (infra/scripts/service-images.sh);
# the publishing half is publish-service-images.sh, run by ci.yml on every green main SHA.
# It is the service chain's counterpart of pull-worker-images.sh, and the one place that
# decides whether a revision's services can be deployed: the deploy runs no service it did
# not pull here, and builds none on the host.
#
# The release protocol is release-chain.sh's, reused rather than repeated:
#
#   1. release_marker_lookup asks the registry API whether the revision is released. Only
#      a typed 404 on the marker's manifest is "absent" (exit 9); anything the registry
#      cannot answer is "not known" (exit 11). Neither pulls a single image.
#   2. release_verify_committed pulls the marker by digest, validates its record with the
#      shared validator (this SHA, this tree's source hash, schema version, exactly the
#      chain, every image by digest in this registry), then pulls every image the record
#      names by digest and requires each to carry this tree's non-empty source hash.
#   3. Only once every image is verified does anything local change: the deployed record
#      rotates (the one it replaces becomes the previous record, which is the rollback
#      target and what cleanup keeps), the new record is written, and every image gets a
#      local name, codegen-orchestrator/<image>:<sha>, so a dangling-image prune can never
#      take the previous release away from a rollback.
#
# Nothing here touches a running container: compose runs the release only when the deploy
# writes its image override from the record this leaves behind
# (scripts/service_release.py compose-override).
#
# Required env vars:
#   GHCR_TOKEN             — a token with packages:read on the owner's packages
#   GHCR_OWNER             — the GitHub org/user that owns the package namespace
#   SERVICE_IMAGE_TAG      — the full git SHA of the revision being deployed
#   DIGEST_FILE            — where the record of the deployed release is kept
#   PREVIOUS_DIGEST_FILE   — where the record it replaces is kept
# Optional env vars:
#   RELEASE_VALIDATION_ONLY=true — look the marker up and validate its record, without
#       pulling a service image, naming one locally or writing a record. This is the
#       read-only probe the pre-deploy wait runs on the GitHub runner
#       (scripts/wait_release.py); DIGEST_FILE and PREVIOUS_DIGEST_FILE are not required.
#
# Exit codes, one per reason so a caller can tell them apart:
#   1   usage: a variable is missing, or the tag is not a full git SHA
#   7   a released image carries a wrong or empty source hash (release-chain.sh)
#   8   the verified release could not be recorded on this host
#   9   this revision has no release marker: it was never released as a whole
#   10  the marker is unreadable, not a valid record of this release, or names an image
#       that is gone (release-chain.sh)
#   11  the registry could not say whether this revision is released (release-chain.sh)

set -euo pipefail

EXIT_USAGE=1
EXIT_RECORD=8
EXIT_NO_RELEASE=9

require() {
    if [ -z "${!1:-}" ]; then
        echo "FATAL: $1 is required: $2" >&2
        exit "${EXIT_USAGE}"
    fi
}
require GHCR_TOKEN "a token with packages:read"
require GHCR_OWNER "the GitHub org/user that owns the package namespace"
require SERVICE_IMAGE_TAG "the full git SHA of the revision being deployed; there is no default"
VALIDATION_ONLY="${RELEASE_VALIDATION_ONLY:-false}"
if [ "${VALIDATION_ONLY}" != true ]; then
    require DIGEST_FILE "where to record the deployed release"
    require PREVIOUS_DIGEST_FILE "where to keep the record it replaces"
fi
if ! [[ "${SERVICE_IMAGE_TAG}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "FATAL: SERVICE_IMAGE_TAG=${SERVICE_IMAGE_TAG} is not a full git SHA." >&2
    echo "       A service release is keyed by the SHA it was built from, nothing else." >&2
    exit "${EXIT_USAGE}"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=infra/scripts/service-images.sh
source "${SCRIPT_DIR}/service-images.sh"

# The single producer of this value, the same one the publisher stamped on every image.
EXPECTED_HASH="$(python3 "${REPO_ROOT}/scripts/shared_freshness.py" hash)"
if [ -z "${EXPECTED_HASH}" ]; then
    echo "FATAL: scripts/shared_freshness.py hash printed nothing; nothing is pulled." >&2
    exit "${EXIT_USAGE}"
fi
REGISTRY="$(release_image_registry "${GHCR_OWNER}")"
MARKER="${REGISTRY}/${SERVICE_RELEASE_MARKER_IMAGE}:${SERVICE_IMAGE_TAG}"
mapfile -t CHAIN < <(service_image_names)

echo "Logging in to GHCR..."
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_OWNER}" --password-stdin

echo "Deployed revision ${SERVICE_IMAGE_TAG} carries ${SERVICE_SOURCE_HASH_LABEL}=${EXPECTED_HASH}"

release_marker_lookup "${MARKER}" "${GHCR_OWNER}" "${GHCR_TOKEN}" pull
case "${RELEASE_MARKER_STATE}" in
    present) ;;
    absent)
        echo "FATAL: ${MARKER} has no release marker (registry manifest returned HTTP 404)." >&2
        echo "       ${SERVICE_IMAGE_TAG} was never released as a whole, whatever image tags" >&2
        echo "       exist, so its services cannot be deployed." >&2
        exit "${EXIT_NO_RELEASE}"
        ;;
    *)
        echo "FATAL: the registry cannot say whether ${SERVICE_IMAGE_TAG} is released" >&2
        echo "       (${MARKER}); see the reason above. Nothing is pulled." >&2
        exit "${RELEASE_EXIT_MARKER_LOOKUP}"
        ;;
esac
marker_reference="${REGISTRY}/${SERVICE_RELEASE_MARKER_IMAGE}@${RELEASE_MARKER_DIGEST}"
echo "Reading the release of ${SERVICE_IMAGE_TAG} from ${marker_reference}..."

if [ "${VALIDATION_ONLY}" = true ]; then
    release_marker_read "${marker_reference}" "${SERVICE_RELEASE_LABEL}" \
        "${SERVICE_IMAGE_TAG}" "${EXPECTED_HASH}" "${REGISTRY}" \
        "${SERVICE_RELEASE_SCHEMA_VERSION}" "${CHAIN[@]}" > /dev/null || exit "$?"
    echo "The service release marker of ${SERVICE_IMAGE_TAG} is valid."
    exit 0
fi

released="$(release_verify_committed "${marker_reference}" "${SERVICE_RELEASE_LABEL}" \
    "${SERVICE_IMAGE_TAG}" "${EXPECTED_HASH}" "${SERVICE_SOURCE_HASH_LABEL}" "${REGISTRY}" \
    "${SERVICE_RELEASE_SCHEMA_VERSION}" "${CHAIN[@]}")" || exit "$?"
mapfile -t verified <<< "${released}"

# Every image is verified; only now does anything on this host change.
if ! python3 "${REPO_ROOT}/scripts/service_release.py" rotate \
    --current-record "${DIGEST_FILE}" --previous-record "${PREVIOUS_DIGEST_FILE}" \
    --next-revision "${SERVICE_IMAGE_TAG}"; then
    echo "FATAL: the previous service release record could not be kept." >&2
    exit "${EXIT_RECORD}"
fi
staged="${DIGEST_FILE}.next"
if ! release_record "${SERVICE_IMAGE_TAG}" "${EXPECTED_HASH}" "${staged}" \
    "${SERVICE_RELEASE_SCHEMA_VERSION}" "${verified[@]}" >&2 \
    || ! mv "${staged}" "${DIGEST_FILE}"; then
    echo "FATAL: the verified release of ${SERVICE_IMAGE_TAG} could not be recorded" >&2
    echo "       in ${DIGEST_FILE}." >&2
    exit "${EXIT_RECORD}"
fi
for record in "${verified[@]}"; do
    image="${record%%=*}"
    reference="${record#*=}"
    if ! docker tag "${reference}" "codegen-orchestrator/${image}:${SERVICE_IMAGE_TAG}"; then
        echo "FATAL: ${reference} could not be named on this host." >&2
        exit "${EXIT_RECORD}"
    fi
done

echo "Service images ready (${SERVICE_IMAGE_TAG}, source hash ${EXPECTED_HASH}):"
for record in "${verified[@]}"; do
    echo "  ${record}"
done
