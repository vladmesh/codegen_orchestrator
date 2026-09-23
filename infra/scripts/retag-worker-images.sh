#!/usr/bin/env bash
# Move the local worker-base-*:latest names to the worker release a deploy verified.
#
# pull-worker-images.sh with RELEASE_DEFER_RETAG=true pulls and verifies the release and
# writes its record, but moves no local name: the production deploy verifies everything
# before it switches the host, and only switches once all of it has passed. This is the
# half it runs after the switch. It names exactly the `<repository>@sha256:...` the record
# holds — the digests pull-worker-images.sh verified — and resolves nothing.
#
# Usage: retag-worker-images.sh <deployed-worker-images.json>
#
# Exit codes:
#   1  usage: no record given
#   2  the record is not a record of exactly this chain, every image by digest
#   3  an image the record names is not on this host

set -euo pipefail

EXIT_USAGE=1
EXIT_BAD_RECORD=2
EXIT_MISSING_IMAGE=3

RECORD="${1:-}"
if [ -z "${RECORD}" ]; then
    echo "usage: $0 <deployed-worker-images.json>" >&2
    exit "${EXIT_USAGE}"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/worker-images.sh
source "${SCRIPT_DIR}/worker-images.sh"

if ! records="$(python3 - "${RECORD}" "${WORKER_BASE_IMAGES[@]}" <<'PY'
import json
import sys

path, chain = sys.argv[1], sys.argv[2:]
try:
    with open(path, encoding="utf-8") as handle:
        images = json.load(handle)["images"]
except (OSError, ValueError, KeyError, TypeError) as error:
    raise SystemExit(f"{path} is not a worker release record ({error})")
if not isinstance(images, dict) or sorted(images) != sorted(chain):
    raise SystemExit(f"{path} does not name exactly the chain {chain}")
for name in chain:
    reference = images[name].get("reference") if isinstance(images[name], dict) else None
    if not isinstance(reference, str) or "@sha256:" not in reference:
        raise SystemExit(f"{path} does not name {name} by digest")
    print(f"{name}={reference}")
PY
)"; then
    echo "FATAL: the deployed worker record cannot be retagged from; see the reason above." >&2
    exit "${EXIT_BAD_RECORD}"
fi

while IFS= read -r record; do
    image="${record%%=*}"
    reference="${record#*=}"
    echo "Retagging ${reference} to ${image}:latest..."
    if ! docker tag "${reference}" "${image}:latest"; then
        echo "FATAL: ${reference} is not on this host; the verified release is gone." >&2
        exit "${EXIT_MISSING_IMAGE}"
    fi
done <<< "${records}"
