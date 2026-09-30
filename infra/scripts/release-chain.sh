#!/usr/bin/env bash
# The release-marker protocol, shared by every image release chain of this repository.
#
# Two chains publish images to GHCR as one release keyed by a git SHA: the worker base
# images (worker-images.sh) and the control-plane service images (service-images.sh).
# The worker chain also releases each source hash once, under a `source-<hash>` key, and
# every SHA's marker names that release; to this protocol both are just keys.
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
#   corruption of a committed release, refused by the caller and never repaired;
# * whether a SHA is released is asked once, of the registry API, and has three
#   answers (release_marker_lookup): present, absent, or not known. Only "absent" may
#   lead to a push; "not known" fails closed before anything is pushed.

# The exit codes of the protocol, the same in every publisher of every chain. Each
# publisher adds its own codes for its build half; these are the reasons that belong to
# the release itself.
#   7   an image of an already-released SHA carries a wrong or empty source hash
#   10  the release marker of this SHA is unreadable, is not a valid record of this
#       release, or names an image that is gone
#   11  the registry could not say whether this SHA is released (authentication,
#       transport, rate limit, an unexpected HTTP answer): nothing is pushed
#   12  building or pushing the release marker failed: the SHA is not released
RELEASE_EXIT_RELEASED_LABEL=7
RELEASE_EXIT_BROKEN_RELEASE=10
RELEASE_EXIT_MARKER_LOOKUP=11
RELEASE_EXIT_MARKER_PUBLISH=12

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

# Is this SHA released? The one question every publisher asks before it pushes
# anything, answered in exactly one of three ways:
#
#   present  the registry serves the marker; RELEASE_MARKER_DIGEST is the one
#            resolution of its tag, and everything after names that digest.
#   absent   the registry positively says the marker is unknown: HTTP 404 on its
#            manifest. The only answer that lets a caller push.
#   error    anything else — credentials refused, transport, rate limit, a 5xx, an
#            unusable token, a marker that answers 200 and then does not resolve. The
#            SHA may well be released, so the caller fails closed before any push.
#
# A failed `buildx imagetools inspect` is not "absent": its wording is not a registry
# contract, and an auth or transport failure reads the same as a missing tag. So the
# registry API is asked directly, the way pull-worker-images.sh asks it. A publisher
# requests the token for `pull,push`: the token `docker push` itself obtains, which is
# issued before the marker's package exists, on the very first release. A consumer
# holds a read-only credential and requests `pull`.
#
# The answer is left in RELEASE_MARKER_STATE and RELEASE_MARKER_DIGEST, and the reason
# of an error on stderr; call it directly, not in a subshell.
#
# Usage: release_marker_lookup <marker_tag_reference> <registry_user> <registry_token> \
#            <token_actions>
release_marker_lookup() {
    local marker="$1" user="$2" password="$3" actions="$4"
    local repository="${marker%:*}" tag="${marker##*:}"
    local path="${repository#ghcr.io/}"
    local workdir status curl_exit token digest
    RELEASE_MARKER_STATE=error
    RELEASE_MARKER_DIGEST=""

    workdir="$(mktemp -d)" || return 0
    chmod 700 "${workdir}"
    printf 'machine ghcr.io\nlogin %s\npassword %s\n' "${user}" "${password}" > "${workdir}/netrc"

    if status="$(curl --silent --show-error --max-time 60 --retry 3 --retry-delay 2 \
        --output "${workdir}/token" --write-out '%{http_code}' \
        --netrc-file "${workdir}/netrc" --get --data-urlencode 'service=ghcr.io' \
        --data-urlencode "scope=repository:${path}:${actions}" https://ghcr.io/token)"; then
        curl_exit=0
    else
        curl_exit=$?
    fi
    if [ "${curl_exit}" -ne 0 ] || [ "${status}" != 200 ]; then
        echo "the registry token request for ${marker} failed (curl exit ${curl_exit}, HTTP ${status:-none})" >&2
        rm -rf "${workdir}"
        return 0
    fi
    if ! token="$(python3 - "${workdir}/token" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    response = json.load(handle)
token = response.get("token") or response.get("access_token")
if not isinstance(token, str) or not token:
    raise SystemExit("the registry token response has no token")
print(token)
PY
    )"; then
        echo "the registry token response for ${marker} is unusable" >&2
        rm -rf "${workdir}"
        return 0
    fi
    printf 'Authorization: Bearer %s\n' "${token}" > "${workdir}/header"

    if status="$(curl --silent --show-error --max-time 60 --retry 3 --retry-delay 2 \
        --output /dev/null --write-out '%{http_code}' --header "@${workdir}/header" \
        --header 'Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json' \
        "https://ghcr.io/v2/${path}/manifests/${tag}")"; then
        curl_exit=0
    else
        curl_exit=$?
    fi
    rm -rf "${workdir}"
    if [ "${curl_exit}" -ne 0 ]; then
        echo "the registry manifest request for ${marker} failed in transport (curl exit ${curl_exit})" >&2
        return 0
    fi
    case "${status}" in
        200)
            if digest="$(release_image_digest "${marker}")" && [ -n "${digest}" ]; then
                RELEASE_MARKER_STATE=present
                RELEASE_MARKER_DIGEST="${digest}"
            else
                echo "the registry serves ${marker}, but its digest does not resolve" >&2
            fi
            ;;
        404)
            RELEASE_MARKER_STATE=absent
            ;;
        *)
            echo "the registry answered HTTP ${status} for the manifest of ${marker}" >&2
            ;;
    esac
    return 0
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
# into a release. Call it only after every image of the chain resolves, and only on the
# "absent" answer of release_marker_lookup.
#
# Returns RELEASE_EXIT_MARKER_PUBLISH when the marker cannot be built or pushed, never
# Docker's own status: a 1 would read as the caller's usage error.
#
# Usage: release_marker_publish <label> <record_name> <marker_reference> <digest_file>
release_marker_publish() {
    local label="$1" record_name="$2" reference="$3" digest_file="$4"
    local context payload
    context="$(mktemp -d)" || return "${RELEASE_EXIT_MARKER_PUBLISH}"
    if ! payload="$(base64 < "${digest_file}" | tr -d '\n')" \
        || ! cp "${digest_file}" "${context}/${record_name}"; then
        echo "the release record ${digest_file} cannot be read into the marker" >&2
        rm -rf "${context}"
        return "${RELEASE_EXIT_MARKER_PUBLISH}"
    fi
    {
        echo "FROM scratch"
        echo "COPY ${record_name} /${record_name}"
        echo "LABEL ${label}=\"${payload}\""
    } > "${context}/Dockerfile"
    if ! docker build -t "${reference}" "${context}" || ! docker push "${reference}"; then
        echo "building or pushing the release marker ${reference} failed" >&2
        rm -rf "${context}"
        return "${RELEASE_EXIT_MARKER_PUBLISH}"
    fi
    rm -rf "${context}"
}

# Read a release marker's payload and print one `<image>=<repository>@<digest>` line
# per image of the chain, in the order the chain is given.
#
# The one validator of a committed record: everything a consumer acts on comes from
# here, so everything is checked here. The record has to be readable, to be the record
# of this SHA and of this tree's source hash, to carry the expected schema version (when
# the chain has one; the worker record predates the field), to name exactly this chain,
# and to name every image in this registry by digest, with its `repository` and `digest`
# fields saying what its `reference` says. Failing any of those is corruption of a
# committed release rather than a retryable state, so this fails non-zero and the
# caller refuses with its own exit code.
#
# Usage: release_marker_images <base64_payload> <git_sha> <source_hash> <registry> \
#            <schema_version> <image>...
release_marker_images() {
    local payload="$1" git_sha="$2" source_hash="$3" registry="$4" schema_version="$5"
    shift 5

    RELEASE_PAYLOAD="${payload}" RELEASE_GIT_SHA="${git_sha}" RELEASE_REGISTRY="${registry}" \
        RELEASE_SOURCE_HASH="${source_hash}" RELEASE_SCHEMA_VERSION="${schema_version}" \
        python3 - "$@" <<'PY'
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
expected_hash = os.environ["RELEASE_SOURCE_HASH"]
expected_schema = os.environ["RELEASE_SCHEMA_VERSION"]

if not expected_hash:
    refuse("no expected source hash was given to check it against")
if not isinstance(record, dict) or not isinstance(record.get("images"), dict):
    refuse("it carries no images map")
if record.get("git_sha") != expected_sha:
    refuse(f"it is the release of {record.get('git_sha')!r}, not of {expected_sha!r}")
if record.get("source_hash") != expected_hash:
    refuse(f"it records source hash {record.get('source_hash')!r}, the tree is {expected_hash!r}")
if expected_schema and record.get("schema_version") != int(expected_schema):
    refuse(f"it has schema version {record.get('schema_version')!r}, not {expected_schema}")

images = record["images"]
if sorted(images) != sorted(chain):
    refuse(f"it names {sorted(images)}, the chain is {sorted(chain)}")

for name in chain:
    entry = images[name]
    if not isinstance(entry, dict):
        refuse(f"{name} is not an image entry ({entry!r})")
    reference = entry.get("reference", "")
    if not isinstance(reference, str):
        refuse(f"{name} has no reference ({reference!r})")
    repository, _, digest = reference.partition("@")
    if repository != f"{registry}/{name}":
        refuse(f"{name} is {repository!r}, which is not {registry}/{name}")
    if not digest.startswith("sha256:"):
        refuse(f"{name} is not named by digest ({reference!r})")
    if entry.get("repository") != repository or entry.get("digest") != digest:
        refuse(
            f"{name} records repository {entry.get('repository')!r} and digest "
            f"{entry.get('digest')!r}, but its reference is {reference!r}"
        )
    print(f"{name}={reference}")
PY
}

# The source hash label of one pulled image on stdout, empty when it has none;
# non-zero when the image cannot be inspected at all.
#
# Usage: release_source_hash_of <reference> <label>
release_source_hash_of() {
    local found
    found="$(docker inspect "$1" --format "{{index .Config.Labels \"$2\"}}")" || return 1
    if [ "${found}" = "<no value>" ]; then
        found=""
    fi
    echo "${found}"
}

# Read the committed record of a SHA whose marker resolved, and print its
# `<image>=<repository>@<digest>` lines on stdout: the marker is pulled by digest and its
# record validated (release_marker_images). No image it names is pulled here; that is
# release_verify_committed, which starts with exactly this. A consumer that only has to
# know whether a revision is released and deployable (a pre-deploy wait) stops here.
#
# Returns 0 or RELEASE_EXIT_BROKEN_RELEASE. Run it in a command substitution.
#
# Usage: release_marker_read <marker_digest_reference> <release_label> <git_sha> \
#            <source_hash> <registry> <schema_version> <image>...
release_marker_read() {
    local marker="$1" release_label="$2" git_sha="$3" source_hash="$4" registry="$5"
    local schema_version="$6"
    shift 6
    local payload

    if ! docker pull "${marker}" >/dev/null; then
        echo "FATAL: the release marker of ${git_sha} (${marker}) cannot be pulled," >&2
        echo "       so the release cannot be re-verified." >&2
        return "${RELEASE_EXIT_BROKEN_RELEASE}"
    fi
    if ! payload="$(docker inspect "${marker}" \
        --format "{{index .Config.Labels \"${release_label}\"}}")"; then
        echo "FATAL: the release marker of ${git_sha} (${marker}) cannot be inspected," >&2
        echo "       so the release cannot be re-verified." >&2
        return "${RELEASE_EXIT_BROKEN_RELEASE}"
    fi
    if ! release_marker_images "${payload}" "${git_sha}" "${source_hash}" \
        "${registry}" "${schema_version}" "$@"; then
        echo "FATAL: the release marker of ${git_sha} does not carry a usable record." >&2
        return "${RELEASE_EXIT_BROKEN_RELEASE}"
    fi
}

# Re-verify the committed release of a SHA whose marker resolved, and print its
# `<image>=<repository>@<digest>` lines on stdout. Every stage of every chain that finds
# the marker present runs exactly this, so none of them can claim a broken release is
# fine: the marker is read (release_marker_read), and every image it names is pulled by
# digest and has to carry this tree's non-empty source hash. Nothing is pushed, whatever
# the outcome.
#
# Returns 0, RELEASE_EXIT_BROKEN_RELEASE (the marker cannot be read, is not a valid
# record of this release, or names an image that is gone) or
# RELEASE_EXIT_RELEASED_LABEL (a named image carries a wrong or empty source hash).
# Progress and reasons go to stderr. Run it in a command substitution and exit with its
# status; set -e does not reach inside, so every step is checked here.
#
# Usage: release_verify_committed <marker_digest_reference> <release_label> <git_sha> \
#            <source_hash> <source_hash_label> <registry> <schema_version> <image>...
release_verify_committed() {
    local marker="$1" release_label="$2" git_sha="$3" source_hash="$4" hash_label="$5"
    local registry="$6" schema_version="$7"
    shift 7
    local released record image reference found

    released="$(release_marker_read "${marker}" "${release_label}" "${git_sha}" \
        "${source_hash}" "${registry}" "${schema_version}" "$@")" || return "$?"

    while IFS= read -r record; do
        image="${record%%=*}"
        reference="${record#*=}"
        if ! docker pull "${reference}" >/dev/null \
            || ! found="$(release_source_hash_of "${reference}" "${hash_label}")"; then
            echo "FATAL: the release of ${git_sha} names ${reference}," >&2
            echo "       which is not in the registry. A committed release is not repaired" >&2
            echo "       here: publish the next commit instead." >&2
            return "${RELEASE_EXIT_BROKEN_RELEASE}"
        fi
        if [ -z "${found}" ] || [ "${found}" != "${source_hash}" ]; then
            echo "FATAL: the released ${image} of ${git_sha} (${reference})" >&2
            echo "       carries ${hash_label}=${found:-(no label)}," >&2
            echo "       the tree is ${source_hash}. A released SHA is never rewritten." >&2
            return "${RELEASE_EXIT_RELEASED_LABEL}"
        fi
        echo "  ${image}: ${hash_label}=${found}" >&2
        echo "${record}"
    done <<< "${released}"
}
