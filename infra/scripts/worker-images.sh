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

# Where the chain lives, given the GitHub org/user that owns the packages.
worker_image_registry() {
    printf 'ghcr.io/%s/codegen-orchestrator' "$1"
}

# What one published tag resolves to in the registry right now, or non-zero if the
# registry cannot resolve it at all.
#
# Every half resolves a tag exactly once and then works from `<repository>@<digest>`:
# the pull, the source-hash check and the record all name that one digest. Two lookups
# of the same mutable tag can answer differently, and then the digest a deploy writes
# down is not provably the image it verified.
worker_image_digest() {
    docker buildx imagetools inspect "$1" --format '{{.Manifest.Digest}}'
}

# Write the machine-readable record of one release: the revision, the source hash its
# images carry, and the digest reference of every image in the chain.
#
# Usage: worker_image_record <git_sha> <source_hash> <digest_file> <image>=<repository>@<digest>...
worker_image_record() {
    local git_sha="$1" source_hash="$2" digest_file="$3"
    shift 3

    GIT_SHA="${git_sha}" SOURCE_HASH="${source_hash}" DIGEST_FILE="${digest_file}" \
        python3 - "$@" <<'PY'
import json
import os
import sys

images = {}
for record in sys.argv[1:]:
    name, _, reference = record.partition("=")
    repository, _, digest = reference.partition("@")
    images[name] = {"reference": reference, "repository": repository, "digest": digest}

path = os.environ["DIGEST_FILE"]
with open(path, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "git_sha": os.environ["GIT_SHA"],
            "source_hash": os.environ["SOURCE_HASH"],
            "images": images,
        },
        handle,
        indent=2,
        sort_keys=True,
    )
    handle.write("\n")
print(f"Wrote {path}")
PY
}
