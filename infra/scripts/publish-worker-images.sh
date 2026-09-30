#!/usr/bin/env bash
# Build the worker base image chain once per source hash, and release it for every green
# commit on main.
#
# The chain is not invented here: `make rebuild-worker-images` already builds common
# and then claude, codex and factory from that exact common, stamping every image
# with `--build-arg SOURCE_HASH`. This script verifies what that build produced, pushes
# it, and commits the release.
#
# Four tag pushes are not one registry transaction, and no shell can make them one.
# So a pushed tag is not a release here: a release is one further object, the release
# marker (infra/scripts/worker-images.sh), written last and carrying the digest record
# of the four images. The puller resolves the marker of its revision first and deploys
# only the digests it names, so a run that dies mid-chain leaves bytes in the registry
# that nothing will ever act on.
#
# What the images bake is exactly the trees of the worker source hash, so the release is
# keyed by that hash. Two markers carry it, in the same record shape:
#
#   worker-base-release:source-<hash>  the release of that content, written once, by the
#                                      first green commit that built it;
#   worker-base-release:<git sha>      the release of one commit, written for every green
#                                      push, naming the digests of its hash's release.
#
# And the work is split in two stages, the way the service chain is
# (publish-service-images.sh):
#
#   candidates  runs beside the suites, before anything is known to be green, which is
#               safe only because a candidate is not a release. It resolves the images
#               this commit would release and writes their record to CANDIDATE_FILE,
#               which CI hands to the DinD suite and to the release stage alike:
#                 the commit is released   -> the digests its marker names;
#                 the hash is released     -> the digests the hash marker names;
#                 neither                  -> build the chain once, push the four
#                                             images under the SHA tag, in parallel,
#                                             and name what was pushed.
#               A released marker is re-verified (release_verify_committed); nothing is
#               built or pushed for it.
#   release     runs only after the Required CI Gate is green. It takes the candidate
#               record CI handed over (WORKER_CANDIDATES) — the digests the DinD suite
#               pulled and tested — and commits exactly those, or refuses:
#                 the commit is released   -> re-verify; it must name the tested digests;
#                 the hash is released     -> re-verify; it must name the tested digests;
#                                             write the commit marker;
#                 neither                  -> pull and label-check every tested digest,
#                                             write the hash marker, then the commit
#                                             marker.
#               It builds nothing and pushes no image: only markers.
#
# Whatever the answer, a marker lookup that cannot say (credentials, transport, 5xx)
# means the key may be released, so nothing is built or pushed (exit 11). Only a
# registry 404 on the marker's manifest means absent (release_marker_lookup in
# release-chain.sh). A marker that resolves but names an image that does not, or one
# built from other sources, is corruption of a committed release: it is refused and
# never repaired, because repairing it would change what an already-deployed release
# means.
#
# Usage: publish-worker-images.sh candidates|release
#
# Required env vars:
#   GHCR_TOKEN         — GitHub token with packages:write scope
#   GHCR_OWNER         — GitHub org/user that owns the package namespace
#   GIT_SHA            — the commit being published; the tag of its candidates and of
#                        its release marker
#   CANDIDATE_FILE     — candidates only: where to write the candidate record
#   WORKER_CANDIDATES  — release only: that record, base64-encoded, as CI handed it over
#   DIGEST_FILE        — release only: where to write the record of the commit's release
#
# Exit codes, one per reason so a caller can tell them apart:
#   1   usage: no stage, an unknown stage, or a missing variable
#   2   a built image or a candidate carries another tree's source hash, or none
#   4   pushing a candidate failed: nothing is released, rerun the candidate stage
#   7   an image of an already-released key carries a wrong or empty source hash
#   8   a tag that was just pushed does not resolve to a digest, or a tested candidate
#       cannot be pulled
#   10  a release marker is unreadable, is not a valid record of its release, or names
#       an image that is gone
#   11  the registry cannot say whether a key is released: nothing is pushed
#   12  building or pushing a release marker failed: the commit is not released
#   13  the handed-over candidate record is not a record of this commit's chain
#   14  the tested candidates are not the digests this commit's release names: rerun
#       the whole workflow, so the candidate stage hands over the committed release
# 7, 10, 11 and 12 are the release protocol's own codes (release-chain.sh), the same in
# every chain.

set -euo pipefail

EXIT_USAGE=1
EXIT_BUILT_LABEL=2
EXIT_PUSH=4
EXIT_UNRESOLVED=8
EXIT_BAD_CANDIDATES=13
EXIT_NOT_TESTED=14

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
if [ "${STAGE}" = candidates ]; then
    require CANDIDATE_FILE "where to write the candidate record"
else
    require WORKER_CANDIDATES "the candidate record the candidate stage handed over"
    require DIGEST_FILE "where to write the published digests"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=infra/scripts/worker-images.sh
source "${SCRIPT_DIR}/worker-images.sh"
EXIT_MARKER_LOOKUP="${RELEASE_EXIT_MARKER_LOOKUP}"

SOURCE_HASH="$(python3 "${REPO_ROOT}/scripts/shared_freshness.py" hash)"
if [ -z "${SOURCE_HASH}" ]; then
    echo "FATAL: scripts/shared_freshness.py hash printed nothing; nothing is published." >&2
    exit "${EXIT_USAGE}"
fi
REGISTRY="$(worker_image_registry "${GHCR_OWNER}")"
SOURCE_KEY="$(worker_source_key "${SOURCE_HASH}")"
COMMIT_MARKER="${REGISTRY}/${WORKER_RELEASE_MARKER_IMAGE}:${GIT_SHA}"
SOURCE_MARKER="${REGISTRY}/${WORKER_RELEASE_MARKER_IMAGE}:${SOURCE_KEY}"

echo "Logging in to GHCR..."
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_OWNER}" --password-stdin

# Ask the registry whether <key> is released. On "present" re-verify that release and
# leave its `<image>=<repository>@<digest>` lines in RELEASED; on "absent" leave
# RELEASED empty. Anything else exits: a lookup that cannot say fails closed before any
# push, and a broken release is refused with the protocol's own code.
#
# Usage: released_as <marker_tag_reference> <key>
released_as() {
    local marker="$1" key="$2"
    RELEASED=""
    release_marker_lookup "${marker}" "${GHCR_OWNER}" "${GHCR_TOKEN}" pull,push
    case "${RELEASE_MARKER_STATE}" in
        present)
            local reference="${REGISTRY}/${WORKER_RELEASE_MARKER_IMAGE}@${RELEASE_MARKER_DIGEST}"
            echo "${key} is released (${reference}); re-verifying it."
            RELEASED="$(worker_release_verify "${reference}" "${key}" "${REGISTRY}" \
                "${SOURCE_HASH}")" || exit "$?"
            ;;
        absent)
            echo "${key} has no release marker (${marker})."
            ;;
        *)
            echo "FATAL: the registry cannot say whether ${key} is released (${marker});" >&2
            echo "       see the reason above. Nothing is pushed: rerun once the registry answers." >&2
            exit "${EXIT_MARKER_LOOKUP}"
            ;;
    esac
}

# Write <lines> as the record of <key> to <file>.
#
# Usage: record_as <key> <file> <lines>
record_as() {
    local records
    mapfile -t records <<< "$3"
    worker_image_record "$1" "${SOURCE_HASH}" "$2" "${records[@]}"
}

if [ "${STAGE}" = candidates ]; then
    # A released commit or a released hash is frozen: its digests are the candidates,
    # and nothing is built or pushed for it.
    released_as "${COMMIT_MARKER}" "${GIT_SHA}"
    if [ -z "${RELEASED}" ]; then
        released_as "${SOURCE_MARKER}" "${SOURCE_KEY}"
    fi
    if [ -n "${RELEASED}" ]; then
        record_as "${GIT_SHA}" "${CANDIDATE_FILE}" "${RELEASED}"
        echo "The candidates of ${GIT_SHA} are the released images of ${SOURCE_KEY}; nothing was built."
        exit 0
    fi

    # Neither is released. Anything of this SHA already in the registry is residue of a
    # run that did not finish, and pushing over it releases nothing by itself.
    echo "Building the worker chain for ${GIT_SHA} (source hash ${SOURCE_HASH})..."
    make -C "${REPO_ROOT}" rebuild-worker-images

    # What was built has to say what it was built from, before any of it is pushed.
    for image in "${WORKER_BASE_IMAGES[@]}"; do
        found="$(docker inspect "${image}:latest" \
            --format "{{index .Config.Labels \"${WORKER_SOURCE_HASH_LABEL}\"}}")"
        if [ "${found}" != "${SOURCE_HASH}" ]; then
            echo "FATAL: ${image}:latest carries ${WORKER_SOURCE_HASH_LABEL}=${found:-(no label)}," >&2
            echo "       the tree is ${SOURCE_HASH}. Nothing is pushed." >&2
            exit "${EXIT_BUILT_LABEL}"
        fi
        echo "  ${image}: ${WORKER_SOURCE_HASH_LABEL}=${found}"
    done

    # The four pushes go in parallel; each is waited for, and one failure fails the stage.
    pids=()
    for image in "${WORKER_BASE_IMAGES[@]}"; do
        remote="${REGISTRY}/${image}:${GIT_SHA}"
        docker tag "${image}:latest" "${remote}"
        echo "Pushing ${remote}..."
        docker push "${remote}" &
        pids+=("$!")
    done
    failed=0
    for pid in "${pids[@]}"; do
        wait "${pid}" || failed=1
    done
    if [ "${failed}" -ne 0 ]; then
        echo "FATAL: pushing a candidate of ${GIT_SHA} failed. Nothing is released;" >&2
        echo "       rerun this job to push its candidates again." >&2
        exit "${EXIT_PUSH}"
    fi

    echo "Recording the candidates..."
    records=()
    for image in "${WORKER_BASE_IMAGES[@]}"; do
        remote="${REGISTRY}/${image}:${GIT_SHA}"
        # The one resolution of this tag: everything downstream names this digest.
        if ! digest="$(worker_image_digest "${remote}")" || [ -z "${digest}" ]; then
            echo "FATAL: ${remote} has no digest in the registry after being pushed." >&2
            exit "${EXIT_UNRESOLVED}"
        fi
        echo "  ${remote} -> ${digest}"
        records+=("${image}=${REGISTRY}/${image}@${digest}")
    done
    worker_image_record "${GIT_SHA}" "${SOURCE_HASH}" "${CANDIDATE_FILE}" "${records[@]}"
    echo "The candidates of ${GIT_SHA} are pushed; the release job commits them once CI is green."
    exit 0
fi

# release: the handed-over record is read with the one validator of a record, so it
# has to name exactly this chain, by digest, in this registry, for this SHA and hash.
if ! TESTED="$(worker_release_images "${WORKER_CANDIDATES}" "${GIT_SHA}" "${REGISTRY}" \
    "${SOURCE_HASH}")"; then
    echo "FATAL: the candidate record handed over for ${GIT_SHA} is not a record of its chain;" >&2
    echo "       see the reason above. Nothing is released." >&2
    exit "${EXIT_BAD_CANDIDATES}"
fi
echo "The tested candidates of ${GIT_SHA}:"
echo "${TESTED}"

# A release that exists already has to be the images that were tested; a release of
# other bytes, however equal their sources, is not what this run vouches for.
#
# Usage: require_tested <key>
require_tested() {
    if [ "${RELEASED}" != "${TESTED}" ]; then
        echo "FATAL: the release of $1 names" >&2
        echo "${RELEASED}" >&2
        echo "       but this run tested" >&2
        echo "${TESTED}" >&2
        echo "       Nothing is written. Rerun the whole workflow: its candidate stage then" >&2
        echo "       hands the committed release to the suites." >&2
        exit "${EXIT_NOT_TESTED}"
    fi
}

released_as "${COMMIT_MARKER}" "${GIT_SHA}"
if [ -n "${RELEASED}" ]; then
    require_tested "${GIT_SHA}"
    record_as "${GIT_SHA}" "${DIGEST_FILE}" "${RELEASED}"
    echo "${GIT_SHA} is already released as the tested images; nothing was pushed."
    exit 0
fi

released_as "${SOURCE_MARKER}" "${SOURCE_KEY}"
if [ -n "${RELEASED}" ]; then
    require_tested "${SOURCE_KEY}"
else
    # A fresh hash: the tested candidates become its release. Each digest is pulled and
    # has to carry this tree's hash before the content key is committed.
    echo "Verifying the candidates of ${SOURCE_KEY}..."
    while IFS= read -r record; do
        image="${record%%=*}"
        reference="${record#*=}"
        if ! docker pull "${reference}" >/dev/null \
            || ! found="$(release_source_hash_of "${reference}" "${WORKER_SOURCE_HASH_LABEL}")"; then
            echo "FATAL: the tested ${image} (${reference}) cannot be pulled. Nothing is released." >&2
            exit "${EXIT_UNRESOLVED}"
        fi
        if [ "${found}" != "${SOURCE_HASH}" ]; then
            echo "FATAL: ${reference} (${image}) carries ${WORKER_SOURCE_HASH_LABEL}=${found:-(no label)}," >&2
            echo "       the tree is ${SOURCE_HASH}. Nothing is released." >&2
            exit "${EXIT_BUILT_LABEL}"
        fi
        echo "  ${image}: ${WORKER_SOURCE_HASH_LABEL}=${found}"
    done <<< "${TESTED}"
    SOURCE_RECORD="$(mktemp)"
    record_as "${SOURCE_KEY}" "${SOURCE_RECORD}" "${TESTED}"
    # The content key is committed first: a run that dies after this write leaves a
    # released hash and an unreleased commit, which a rerun completes as an alias.
    echo "Publishing the release marker ${SOURCE_MARKER}..."
    worker_release_marker_publish "${SOURCE_MARKER}" "${SOURCE_RECORD}" || exit "$?"
    rm -f "${SOURCE_RECORD}"
fi

# The commit's record names the digests of its hash's release, which are the tested
# ones. This last write is the release of the commit: before it nothing may deploy this
# SHA; after it nothing may change it.
record_as "${GIT_SHA}" "${DIGEST_FILE}" "${TESTED}"
echo "Publishing the release marker ${COMMIT_MARKER}..."
worker_release_marker_publish "${COMMIT_MARKER}" "${DIGEST_FILE}" || exit "$?"
echo "${GIT_SHA} is released as ${SOURCE_KEY}."
