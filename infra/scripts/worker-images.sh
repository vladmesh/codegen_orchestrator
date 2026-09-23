#!/usr/bin/env bash
# The worker base image release chain, named once.
#
# Sourced by the two halves that have to agree on it: publish-worker-images.sh
# (build and push) and pull-worker-images.sh (pull and verify on the deployment
# host). Listing the chain in one place is what keeps a fifth image from being
# published and never verified, or verified and never published.
#
# Build order: common first, then the agent images built from that exact common.

WORKER_BASE_IMAGES=(
    "worker-base-common"
    "worker-base-claude"
    "worker-base-factory"
    "worker-base-codex"
)

# Set on every image by --build-arg SOURCE_HASH, and read back at runtime by
# worker-manager (services/worker-manager/src/image_builder.py). The value itself
# has exactly one producer: scripts/shared_freshness.py.
WORKER_SOURCE_HASH_LABEL="org.codegen.worker_source_hash"

# The release marker: the commit point of a SHA's release, and the only thing that
# says a release exists.
#
# Four tag pushes cannot be one registry transaction, so image tags alone can never
# mean "released": a run that dies between the second and the third push leaves tags
# behind, and those bytes must not be deployable. So the publisher pushes the four
# images first and then, only once all four resolve, writes one more object — this
# marker, tagged with the same git SHA, carrying the digest record of that release.
# That single write is the release. The puller resolves the marker before anything
# else and deploys only the digests the marker names; leftover image tags with no
# marker are inert residue, and a rerun of the publish job may push over them.
WORKER_RELEASE_MARKER_IMAGE="worker-base-release"

# Where the marker carries the record: the same JSON `worker_image_record` writes,
# base64-encoded so it survives a Dockerfile LABEL unquoted and unescaped.
WORKER_RELEASE_LABEL="org.codegen.worker_release"

# The marker protocol itself is shared with the service image chain.
# shellcheck source=infra/scripts/release-chain.sh
source "$(dirname "${BASH_SOURCE[0]}")/release-chain.sh"

# Where the chain lives, given the GitHub org/user that owns the packages.
worker_image_registry() {
    release_image_registry "$1"
}

# What one published tag resolves to in the registry right now, or non-zero if the
# registry cannot resolve it at all.
#
# Every half resolves a tag exactly once and then works from `<repository>@<digest>`:
# the pull, the source-hash check and the record all name that one digest. Two lookups
# of the same mutable tag can answer differently, and then the digest a deploy writes
# down is not provably the image it verified.
worker_image_digest() {
    release_image_digest "$1"
}

# Write the machine-readable record of one release: the revision, the source hash its
# images carry, and the digest reference of every image in the chain.
#
# Usage: worker_image_record <git_sha> <source_hash> <digest_file> <image>=<repository>@<digest>...
worker_image_record() {
    local git_sha="$1" source_hash="$2" digest_file="$3"
    shift 3
    release_record "${git_sha}" "${source_hash}" "${digest_file}" "" "$@"
}

# Publish the release marker for one SHA: the single registry write that turns four
# pushed tags into a release. Call it only after every image of the chain resolves.
#
# Usage: worker_release_marker_publish <marker_reference> <digest_file>
worker_release_marker_publish() {
    release_marker_publish "${WORKER_RELEASE_LABEL}" worker-images.json "$1" "$2"
}

# Read a release marker's payload and print one `<image>=<repository>@<digest>` line
# per image of the chain, in build order; non-zero when the marker is not a usable
# record of exactly this chain for this SHA and this tree's source hash
# (release_marker_images).
#
# Usage: worker_release_images <base64_payload> <git_sha> <registry> <source_hash>
worker_release_images() {
    release_marker_images "$1" "$2" "$4" "$3" "" "${WORKER_BASE_IMAGES[@]}"
}

# Re-verify the committed worker release of a SHA whose marker resolved
# (release_verify_committed), printing its `<image>=<repository>@<digest>` lines.
#
# Usage: worker_release_verify <marker_digest_reference> <git_sha> <registry> <source_hash>
worker_release_verify() {
    release_verify_committed "$1" "${WORKER_RELEASE_LABEL}" "$2" "$4" \
        "${WORKER_SOURCE_HASH_LABEL}" "$3" "" "${WORKER_BASE_IMAGES[@]}"
}
