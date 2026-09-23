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

# Whether a simulation input asks for its failure. The action always sets the variable,
# but only a workflow_dispatch run fills it: a push or pull_request run has no dispatch
# inputs, so ci.yml hands the action an empty string, and empty means not requested.
requested() {
    local name=$1 value=${!1?"$1 is required"}
    case "$value" in
        true) return 0 ;;
        false | "") return 1 ;;
        *)
            echo "bootstrap: $name is '$value', not true, false or empty" >&2
            exit 2
            ;;
    esac
}

# Read on every attempt, so a value that is none of these fails them all.
simulate_registry_failure=false simulate_pull_hang=false
if requested SIMULATE_REGISTRY_FAILURE; then simulate_registry_failure=true; fi
if requested SIMULATE_PULL_HANG; then simulate_pull_hang=true; fi

if [ "$attempt" = 1 ] && [ "$simulate_registry_failure" = true ]; then
    echo "::error title=Simulated CI infrastructure failure::Docker image registry is unavailable for Buildx attempt 1."
    exit 1
fi
if [ "$attempt" = 1 ] && [ "$simulate_pull_hang" = true ]; then
    echo "Simulated CI infrastructure failure: the buildkit pull of Buildx attempt 1 hangs."
    exec sleep infinity
fi

builder="ci-builder-${attempt}"
docker buildx create --name "$builder" --driver docker-container \
    --buildkitd-flags '--allow-insecure-entitlement security.insecure --allow-insecure-entitlement network.host' \
    --use
docker buildx inspect --bootstrap "$builder"
echo "BUILDX_BUILDER=${builder}" >>"${GITHUB_ENV:?GITHUB_ENV is required}"
