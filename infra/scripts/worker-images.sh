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

# Publish the release marker for one SHA: the single registry write that turns four
# pushed tags into a release. Call it only after every image of the chain resolves.
#
# Usage: worker_release_marker_publish <marker_reference> <digest_file>
worker_release_marker_publish() {
    local reference="$1" digest_file="$2"
    local context payload
    context="$(mktemp -d)"
    payload="$(base64 < "${digest_file}" | tr -d '\n')"
    cp "${digest_file}" "${context}/worker-images.json"
    {
        echo "FROM scratch"
        echo "COPY worker-images.json /worker-images.json"
        echo "LABEL ${WORKER_RELEASE_LABEL}=\"${payload}\""
    } > "${context}/Dockerfile"
    docker build -t "${reference}" "${context}"
    docker push "${reference}"
    rm -rf "${context}"
}

# Read a release marker's payload and print one `<image>=<repository>@<digest>` line
# per image of the chain, in build order.
#
# Everything a consumer acts on comes from here, so everything is checked here: the
# record has to be readable, to be the record of this SHA, to name exactly this chain
# and to name images in this registry. A marker failing any of those is corruption of
# a committed release rather than a retryable state, so this fails non-zero and the
# caller refuses with its own exit code.
#
# Usage: worker_release_images <base64_payload> <git_sha> <registry>
worker_release_images() {
    local payload="$1" git_sha="$2" registry="$3"

    RELEASE_PAYLOAD="${payload}" RELEASE_GIT_SHA="${git_sha}" RELEASE_REGISTRY="${registry}" \
        RELEASE_CHAIN="${WORKER_BASE_IMAGES[*]}" python3 - <<'PY'
import base64
import binascii
import json
import os
import sys


def refuse(message):
    print(f"the release marker is not a usable record: {message}", file=sys.stderr)
    raise SystemExit(1)


try:
    record = json.loads(base64.b64decode(os.environ["RELEASE_PAYLOAD"], validate=True))
except (binascii.Error, ValueError, UnicodeDecodeError) as error:
    refuse(f"it does not decode ({error})")

chain = os.environ["RELEASE_CHAIN"].split()
registry = os.environ["RELEASE_REGISTRY"]
expected_sha = os.environ["RELEASE_GIT_SHA"]

if not isinstance(record, dict) or not isinstance(record.get("images"), dict):
    refuse("it carries no images map")
if record.get("git_sha") != expected_sha:
    refuse(f"it is the release of {record.get('git_sha')!r}, not of {expected_sha!r}")

images = record["images"]
if sorted(images) != sorted(chain):
    refuse(f"it names {sorted(images)}, the chain is {sorted(chain)}")

for name in chain:
    reference = images[name].get("reference", "")
    repository, _, digest = reference.partition("@")
    if repository != f"{registry}/{name}":
        refuse(f"{name} is {repository!r}, which is not {registry}/{name}")
    if not digest.startswith("sha256:"):
        refuse(f"{name} is not named by digest ({reference!r})")
    print(f"{name}={reference}")
PY
}
