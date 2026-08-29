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
#  13  registry tooling failed or an endpoint returned an unexpected HTTP response

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

# The registry API gives one typed answer about this exact marker before Docker
# resolves it. A 404 on this manifest endpoint is the sole missing-release
# admission. Buildx stderr is deliberately not parsed: its wording is not a
# registry contract and cannot authorize a local build.
NETRC_FILE="$(mktemp)"
TOKEN_FILE="$(mktemp)"
MANIFEST_FILE="$(mktemp)"
AUTH_HEADER_FILE="$(mktemp)"
cleanup_registry_files() {
    rm -f "${NETRC_FILE}" "${TOKEN_FILE}" "${MANIFEST_FILE}" "${AUTH_HEADER_FILE}"
}
trap cleanup_registry_files EXIT
chmod 600 "${NETRC_FILE}" "${AUTH_HEADER_FILE}"
printf 'machine ghcr.io\nlogin %s\npassword %s\n' "${GHCR_OWNER}" "${GHCR_TOKEN}" > "${NETRC_FILE}"

classify_registry_response() {
    # Both registry reads use this complete response matrix. A marker 404 is
    # meaningful because it names the exact release marker; a token 404 is not.
    local endpoint="$1"
    local permits_missing_marker="$2"
    local curl_exit="$3"
    local http_status="$4"

    if [ "${curl_exit}" -ne 0 ]; then
        case "${curl_exit}" in
            2|126|127)
                echo "FATAL: registry ${endpoint} request failed because curl tooling exited ${curl_exit}" >&2
                exit "${EXIT_REGISTRY_TOOL}"
                ;;
            *)
                echo "FATAL: registry ${endpoint} request failed in transport (curl exited ${curl_exit})" >&2
                exit "${EXIT_REGISTRY_TRANSPORT}"
                ;;
        esac
    fi

    case "${http_status}" in
        200) ;;
        404)
            if [ "${permits_missing_marker}" = "true" ]; then
                echo "FATAL: ${MARKER} has no release marker (registry manifest returned HTTP 404)" >&2
                exit "${EXIT_NO_RELEASE}"
            fi
            echo "FATAL: registry ${endpoint} endpoint returned unexpected HTTP 404 for ${MARKER}" >&2
            exit "${EXIT_REGISTRY_TOOL}"
            ;;
        401|403)
            echo "FATAL: registry rejected credentials while reading ${MARKER}" >&2
            exit "${EXIT_REGISTRY_AUTH}"
            ;;
        429)
            echo "FATAL: registry rate-limited the release lookup for ${MARKER}" >&2
            exit "${EXIT_REGISTRY_RATE_LIMIT}"
            ;;
        *)
            echo "FATAL: registry ${endpoint} endpoint returned unexpected HTTP ${http_status} for ${MARKER}" >&2
            exit "${EXIT_REGISTRY_TOOL}"
            ;;
    esac
}

marker_repository="${MARKER%:*}"
marker_path="${marker_repository#ghcr.io/}"
if token_status="$(curl --silent --show-error --output "${TOKEN_FILE}" --write-out '%{http_code}' \
    --netrc-file "${NETRC_FILE}" --get --data-urlencode 'service=ghcr.io' \
    --data-urlencode "scope=repository:${marker_path}:pull" https://ghcr.io/token)"; then
    token_curl_exit=0
else
    token_curl_exit=$?
fi
classify_registry_response "token" false "${token_curl_exit}" "${token_status}"
registry_token="$(python3 - "${TOKEN_FILE}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    response = json.load(handle)
token = response.get("token") or response.get("access_token")
if not isinstance(token, str) or not token:
    raise SystemExit("registry token response has no token")
print(token)
PY
)" || {
    echo "FATAL: registry token response for ${MARKER} was unusable" >&2
    exit "${EXIT_REGISTRY_TOOL}"
}
printf 'Authorization: Bearer %s\n' "${registry_token}" > "${AUTH_HEADER_FILE}"
if manifest_status="$(curl --silent --show-error --output "${MANIFEST_FILE}" --write-out '%{http_code}' \
    --header "@${AUTH_HEADER_FILE}" \
    --header 'Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json' \
    "https://ghcr.io/v2/${marker_path}/manifests/${WORKER_IMAGE_TAG}")"; then
    manifest_curl_exit=0
else
    manifest_curl_exit=$?
fi
classify_registry_response "manifest" true "${manifest_curl_exit}" "${manifest_status}"

# The typed API has proved the marker exists. Resolve its digest once for the
# immutable reference used by every subsequent Docker operation.
if ! marker_digest="$(worker_image_digest "${MARKER}")" || [ -z "${marker_digest}" ]; then
    echo "FATAL: could not resolve digest for confirmed marker ${MARKER}" >&2
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
