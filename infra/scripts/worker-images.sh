#!/usr/bin/env bash
# The worker base image release chain, named once.
#
# Sourced by the three halves that have to agree on it: publish-worker-images.sh
# (build and push), pull-worker-images.sh (pull and verify on the deployment host)
# and record-worker-image-digests.sh (write down what a SHA resolves to). Listing
# the chain in one place is what keeps a fifth image from being published and never
# verified, or verified and never published.
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
