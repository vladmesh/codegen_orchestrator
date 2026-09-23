#!/usr/bin/env bash
# The release-marker protocol, shared by every image release chain of this repository.
#
# Two chains publish images to GHCR as one release keyed by a git SHA: the worker base
# images (worker-images.sh) and the control-plane service images (service-images.sh).
# Each chain names its own images, marker and label; what a release *is* is decided
# here, once, so the two cannot drift apart:
#
# * a chain's image tags are candidates, never a release: several tag pushes cannot be
#   one registry transaction;
# * the release of a SHA is one further object, the release marker, written last and
#   only once every image of the chain resolves. It carries the digest record of the
#   release as a base64 JSON label, so it survives a Dockerfile LABEL unquoted;
# * every tag is resolved exactly once, and everything after that — pull, label check,
#   record — names the `<repository>@<digest>` that one resolution returned;
# * a marker that is unreadable, of another SHA, or names another set of images is
#   corruption of a committed release, refused by the caller and never repaired.

# Where every chain lives, given the GitHub org/user that owns the packages.
release_image_registry() {
    printf 'ghcr.io/%s/codegen-orchestrator' "$1"
}

# What one published tag resolves to in the registry right now, or non-zero if the
# registry cannot resolve it at all.
#
# Two lookups of the same mutable tag can answer differently, and then the digest a
# record writes down is not provably the image that was verified; so resolve once.
release_image_digest() {
    docker buildx imagetools inspect "$1" --format '{{.Manifest.Digest}}'
}

# Write the machine-readable record of one release: the revision, the source hash its
# images carry, and the digest reference of every image in the chain. A non-empty
# schema version is written as `schema_version`; the worker chain's record predates the
# field and its consumers compare the record whole, so it passes an empty one.
#
# Usage: release_record <git_sha> <source_hash> <digest_file> <schema_version> \
#            <image>=<repository>@<digest>...
release_record() {
    local git_sha="$1" source_hash="$2" digest_file="$3" schema_version="$4"
    shift 4

    GIT_SHA="${git_sha}" SOURCE_HASH="${source_hash}" DIGEST_FILE="${digest_file}" \
        SCHEMA_VERSION="${schema_version}" python3 - "$@" <<'PY'
import json
import os
import sys

images = {}
for record in sys.argv[1:]:
    name, _, reference = record.partition("=")
    repository, _, digest = reference.partition("@")
    images[name] = {"reference": reference, "repository": repository, "digest": digest}

document = {
    "git_sha": os.environ["GIT_SHA"],
    "source_hash": os.environ["SOURCE_HASH"],
    "images": images,
}
if os.environ["SCHEMA_VERSION"]:
    document["schema_version"] = int(os.environ["SCHEMA_VERSION"])

path = os.environ["DIGEST_FILE"]
with open(path, "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2, sort_keys=True)
    handle.write("\n")
print(f"Wrote {path}")
PY
}

# Publish a release marker: the single registry write that turns a chain's pushed tags
# into a release. Call it only after every image of the chain resolves.
#
# Usage: release_marker_publish <label> <record_name> <marker_reference> <digest_file>
release_marker_publish() {
    local label="$1" record_name="$2" reference="$3" digest_file="$4"
    local context payload
    context="$(mktemp -d)"
    payload="$(base64 < "${digest_file}" | tr -d '\n')"
    cp "${digest_file}" "${context}/${record_name}"
    {
        echo "FROM scratch"
        echo "COPY ${record_name} /${record_name}"
        echo "LABEL ${label}=\"${payload}\""
    } > "${context}/Dockerfile"
    docker build -t "${reference}" "${context}"
    docker push "${reference}"
    rm -rf "${context}"
}

# Read a release marker's payload and print one `<image>=<repository>@<digest>` line
# per image of the chain, in the order the chain is given.
#
# Everything a consumer acts on comes from here, so everything is checked here: the
# record has to be readable, to be the record of this SHA, to carry the expected schema
# version (when the chain has one), to name exactly this chain and to name images in
# this registry. Failing any of those is corruption of a committed release rather than a
# retryable state, so this fails non-zero and the caller refuses with its own exit code.
#
# Usage: release_marker_images <base64_payload> <git_sha> <registry> <schema_version> <image>...
release_marker_images() {
    local payload="$1" git_sha="$2" registry="$3" schema_version="$4"
    shift 4

    RELEASE_PAYLOAD="${payload}" RELEASE_GIT_SHA="${git_sha}" RELEASE_REGISTRY="${registry}" \
        RELEASE_SCHEMA_VERSION="${schema_version}" python3 - "$@" <<'PY'
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

chain = sys.argv[1:]
registry = os.environ["RELEASE_REGISTRY"]
expected_sha = os.environ["RELEASE_GIT_SHA"]
expected_schema = os.environ["RELEASE_SCHEMA_VERSION"]

if not isinstance(record, dict) or not isinstance(record.get("images"), dict):
    refuse("it carries no images map")
if record.get("git_sha") != expected_sha:
    refuse(f"it is the release of {record.get('git_sha')!r}, not of {expected_sha!r}")
if expected_schema and record.get("schema_version") != int(expected_schema):
    refuse(f"it has schema version {record.get('schema_version')!r}, not {expected_schema}")

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
