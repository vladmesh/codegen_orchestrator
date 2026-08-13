#!/usr/bin/env bash
# Write down what one published SHA of the worker chain resolves to in the registry.
#
# The digests are read out of the registry, not off a local daemon, so this is also
# the check that the whole chain is there under that tag: an image the registry
# cannot resolve fails here instead of being discovered by a worker that dies.
#
# Used twice, on purpose from the same place: by publish-worker-images.sh after it
# pushes, and by the production deploy, which records the release it deployed.
#
# Required env vars:
#   GHCR_TOKEN   — GitHub token with packages:read scope
#   GHCR_OWNER   — GitHub org/user that owns the package namespace
#   GIT_SHA      — the published commit, which is also the image tag
#   SOURCE_HASH  — the source hash that release carries
#   DIGEST_FILE  — where to write the JSON record

set -euo pipefail

: "${GHCR_TOKEN:?GHCR_TOKEN is required}"
: "${GHCR_OWNER:?GHCR_OWNER is required}"
: "${GIT_SHA:?GIT_SHA is required: the published commit, which is also the tag}"
: "${SOURCE_HASH:?SOURCE_HASH is required: the source hash the release carries}"
: "${DIGEST_FILE:?DIGEST_FILE is required: where to write the JSON record}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/worker-images.sh
source "${SCRIPT_DIR}/worker-images.sh"

REGISTRY="$(worker_image_registry "${GHCR_OWNER}")"

echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_OWNER}" --password-stdin

records=()
for image in "${WORKER_BASE_IMAGES[@]}"; do
    remote="${REGISTRY}/${image}:${GIT_SHA}"
    digest="$(docker buildx imagetools inspect "${remote}" --format '{{.Manifest.Digest}}')"
    if [ -z "${digest}" ]; then
        echo "FATAL: ${remote} has no digest in the registry." >&2
        exit 1
    fi
    echo "  ${remote} -> ${digest}"
    records+=("${image}=${remote}@${digest}")
done

python3 - "${records[@]}" <<'PY'
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
