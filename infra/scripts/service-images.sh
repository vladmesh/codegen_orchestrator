#!/usr/bin/env bash
# The control-plane service image release chain, named once.
#
# Sourced by every half that has to agree on it: publish-service-images.sh (build the
# candidates, then commit the release) and pull-service-images.sh (the deploy's puller).
# Listing the images in one place is what keeps an image from being published and never
# verified, or verified and never published.
#
# One entry per Dockerfile, not per compose service: the langgraph image serves
# langgraph, architect, engineering-worker, deploy-worker and qa-worker, and the
# scheduler image serves the three schedulers, so each is published once.
# tests/unit/test_service_image_release_chain.py fails when a production Dockerfile or a
# docker-compose.yml build target is missing here, or listed twice.
#
# Fields: <image> <dockerfile> <build context>. The image name is the one compose gives
# the local build (codegen-orchestrator/<image>), or the service name where compose
# gives none.

SERVICE_IMAGES=(
    "api services/api/Dockerfile ."
    "langgraph services/langgraph/Dockerfile ."
    "scheduler services/scheduler/Dockerfile ."
    "infra-service services/infra-service/Dockerfile ."
    "telegram_bot services/telegram_bot/Dockerfile ."
    "worker-manager services/worker-manager/Dockerfile ."
    "worker-broker services/worker-broker/Dockerfile ."
    "scaffolder services/scaffolder/Dockerfile ."
    "admin-frontend services/admin-frontend/Dockerfile services/admin-frontend"
    "user-dashboard services/user-dashboard/Dockerfile services/user-dashboard"
)

# Set on every image by --build-arg SOURCE_HASH (the same label the worker chain
# carries). The value has exactly one producer: scripts/shared_freshness.py hash.
SERVICE_SOURCE_HASH_LABEL="org.codegen.worker_source_hash"

# The release marker: the commit point of a SHA's service release, and the only thing
# that says one exists (release-chain.sh). The image tags it names are candidates until
# it is written, and inert residue if it never is.
SERVICE_RELEASE_MARKER_IMAGE="service-release"

# Where the marker carries the record: the JSON release_record writes, base64-encoded.
SERVICE_RELEASE_LABEL="org.codegen.service_release"

# The version of that record. A consumer refuses a marker of any other version, so a
# change of its shape is a new number, never a silent reinterpretation.
SERVICE_RELEASE_SCHEMA_VERSION=1

# Where the candidate builds keep their buildx layer cache, one tag per image. This is
# a build cache, not an image anybody runs: it lives in its own repository so that no
# mutable tag sits next to the released images.
SERVICE_BUILD_CACHE_IMAGE="service-build-cache"

# shellcheck source=infra/scripts/release-chain.sh
source "$(dirname "${BASH_SOURCE[0]}")/release-chain.sh"

# The image names of the chain, in listed order.
service_image_names() {
    local entry
    for entry in "${SERVICE_IMAGES[@]}"; do
        echo "${entry%% *}"
    done
}
