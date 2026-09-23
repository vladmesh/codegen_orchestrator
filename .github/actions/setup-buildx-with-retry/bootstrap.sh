#!/usr/bin/env bash
# One attempt at a Buildx builder: create a docker-container builder and boot it, which
# pulls moby/buildkit. scripts/ci-infra.sh retry runs it under a time bound and names the
# attempt in CI_INFRA_ATTEMPT; each attempt gets its own builder, so one that a bound
# stopped half-created is never reused.
#
# The flags are the ones docker/setup-buildx-action gave the builder before this script
# replaced it.

set -euo pipefail

attempt=${CI_INFRA_ATTEMPT:?CI_INFRA_ATTEMPT is required}

if [ "$attempt" = 1 ]; then
    if [ "${SIMULATE_REGISTRY_FAILURE:?}" = true ]; then
        echo "::error title=Simulated CI infrastructure failure::Docker image registry is unavailable for Buildx attempt 1."
        exit 1
    fi
    if [ "${SIMULATE_PULL_HANG:?}" = true ]; then
        echo "Simulated CI infrastructure failure: the buildkit pull of Buildx attempt 1 hangs."
        exec sleep infinity
    fi
fi

builder="ci-builder-${attempt}"
docker buildx create --name "$builder" --driver docker-container \
    --buildkitd-flags '--allow-insecure-entitlement security.insecure --allow-insecure-entitlement network.host' \
    --use
docker buildx inspect --bootstrap "$builder"
echo "BUILDX_BUILDER=${builder}" >>"${GITHUB_ENV:?GITHUB_ENV is required}"
