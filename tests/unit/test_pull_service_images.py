"""The deploy runs only the service release of the revision it deploys, and refuses the rest.

`infra/scripts/pull-service-images.sh` is the consuming half of the service image release
chain, the counterpart of `pull-worker-images.sh` (tests/unit/test_pull_worker_images.py).
These tests run it for real against a fake `docker` and `curl` on PATH, so they need
neither a registry nor a daemon: what is exercised is what it asks the registry, which
digests it pulls, what it does with the source hash label it finds, what it records, and
that every refusal comes before anything local moves.

The record the fake marker carries is the real one the push-to-main CI run of main's
7f93d8b7 published (tests/unit/fixtures/service-release-7f93d8b7.json): its shape, image
set and references are what the puller reads in production.
"""

import base64
import copy
import json
from pathlib import Path
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PULL_SCRIPT = REPO_ROOT / "infra" / "scripts" / "pull-service-images.sh"
REAL_RECORD = json.loads(
    (Path(__file__).parent / "fixtures" / "service-release-7f93d8b7.json").read_text()
)
RELEASED_SHA = REAL_RECORD["git_sha"]
OWNER = "vladmesh"
CHAIN = sorted(REAL_RECORD["images"])
OTHER_SHA = "0123456789abcdef0123456789abcdef01234567"

EXIT_USAGE = 1
EXIT_RELEASED_LABEL = 7
EXIT_RECORD = 8
EXIT_NO_RELEASE = 9
EXIT_BROKEN_RELEASE = 10
EXIT_MARKER_LOOKUP = 11

# A fake docker. `buildx imagetools inspect` resolves the marker tag to one digest
# (FAKE_MARKER_UNRESOLVED makes it fail); `pull` fails for FAKE_UNPULLABLE_IMAGE; `inspect`
# answers the marker payload FAKE_MARKER for the release label and otherwise the source
# hash label, FAKE_LABEL_DEFAULT or, for FAKE_ODD_IMAGE, FAKE_ODD_LABEL. `tag` fails for
# FAKE_UNTAGGABLE_IMAGE. Every call is appended to FAKE_DOCKER_LOG.
FAKE_DOCKER = r"""#!/usr/bin/env bash
set -uo pipefail
command="$1"
shift
echo "${command} $*" >> "${FAKE_DOCKER_LOG}"

image_of() {
    local reference="${1##*/}"
    reference="${reference%%@*}"
    echo "${reference%%:*}"
}

case "${command}" in
    login)
        cat > /dev/null
        ;;
    buildx)
        if [ -n "${FAKE_MARKER_UNRESOLVED:-}" ]; then
            echo "ERROR: $3: not found" >&2
            exit 1
        fi
        echo "sha256:$(image_of "$3")"
        ;;
    pull)
        if [ "$(image_of "$1")" = "${FAKE_UNPULLABLE_IMAGE:-}" ]; then
            echo "ERROR: $1: manifest unknown" >&2
            exit 1
        fi
        ;;
    inspect)
        if [[ "$*" == *service_release* ]]; then
            echo "${FAKE_MARKER}"
        elif [ "$(image_of "$1")" = "${FAKE_ODD_IMAGE:-}" ]; then
            echo "${FAKE_ODD_LABEL}"
        else
            echo "${FAKE_LABEL_DEFAULT}"
        fi
        ;;
    tag)
        if [ "$(image_of "$1")" = "${FAKE_UNTAGGABLE_IMAGE:-}" ]; then
            exit 1
        fi
        ;;
    *)
        echo "fake docker: unexpected command ${command}" >&2
        exit 99
        ;;
esac
"""

# The registry API release_marker_lookup asks: the token endpoint, then the marker's
# manifest. FAKE_*_HTTP_STATUS and FAKE_MARKER_CURL_EXIT inject what a registry answers.
FAKE_CURL = r"""#!/usr/bin/env bash
set -uo pipefail
echo "$*" >> "${FAKE_CURL_LOG}"

output=""
url=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --output) output="$2"; shift 2 ;;
        http*) url="$1"; shift ;;
        *) shift ;;
    esac
done

if [[ "${url}" == *"/token"* ]]; then
    status="${FAKE_TOKEN_HTTP_STATUS:-200}"
    if [ "${status}" = 200 ]; then
        printf '{"token":"fake-registry-token"}' > "${output}"
    fi
    printf '%s' "${status}"
    exit 0
fi
if [ -n "${FAKE_MARKER_CURL_EXIT:-}" ]; then
    exit "${FAKE_MARKER_CURL_EXIT}"
fi
printf '%s' "${FAKE_MARKER_HTTP_STATUS:-200}"
"""


@pytest.fixture(scope="module")
def tree_source_hash() -> str:
    """The hash of this checkout, from the one place that computes it."""
    result = subprocess.run(
        ["python3", str(REPO_ROOT / "scripts" / "shared_freshness.py"), "hash"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def released_record(source_hash: str) -> dict:
    """The real 7f93d8b7 record, with the source hash of the tree under test."""
    record = copy.deepcopy(REAL_RECORD)
    record["source_hash"] = source_hash
    return record


def payload(record: dict) -> str:
    return base64.b64encode(json.dumps(record).encode()).decode()


class Host:
    """A deploy host directory, and the puller run against the fake registry."""

    def __init__(self, tmp_path: Path, source_hash: str) -> None:
        self.root = tmp_path
        self.source_hash = source_hash
        binaries = tmp_path / "bin"
        binaries.mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
            (binaries / name).write_text(body)
            (binaries / name).chmod(0o755)
        self.binaries = binaries
        self.log = tmp_path / "docker.log"
        self.curl_log = tmp_path / "curl.log"
        self.record = tmp_path / "deployed-service-images.json"
        # The puller is never given one; this proves it never writes one either.
        self.previous = tmp_path / "previous-deployed-service-images.json"

    def run(self, **overrides) -> tuple[subprocess.CompletedProcess, list[str]]:
        self.log.write_text("")
        self.curl_log.write_text("")
        environment = {
            "PATH": f"{self.binaries}:/usr/bin:/bin",
            "HOME": str(self.root),
            "FAKE_DOCKER_LOG": str(self.log),
            "FAKE_CURL_LOG": str(self.curl_log),
            "FAKE_LABEL_DEFAULT": self.source_hash,
            "FAKE_MARKER": payload(released_record(self.source_hash)),
            "GHCR_TOKEN": "test-token",
            "GHCR_OWNER": OWNER,
            "SERVICE_IMAGE_TAG": RELEASED_SHA,
            "DIGEST_FILE": str(self.record),
        }
        environment.update(overrides)
        environment = {key: value for key, value in environment.items() if value is not None}
        result = subprocess.run(
            ["bash", str(PULL_SCRIPT)],
            capture_output=True,
            text=True,
            env=environment,
            cwd=self.root,  # the script finds its own repository, not the cwd's
        )
        return result, self.log.read_text().splitlines()


@pytest.fixture
def host(tmp_path, tree_source_hash) -> Host:
    return Host(tmp_path, tree_source_hash)


def _image_pulls(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("pull ") and "service-release" not in call]


def _tags(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("tag ")]


def _nothing_moved(host: Host, calls: list[str], *, record_before: str | None = None) -> None:
    assert not _tags(calls), "a refusal must not name a single image locally"
    if record_before is None:
        assert not host.record.exists(), "a refusal must not write a deployed record"
    else:
        assert host.record.read_text() == record_before
    assert not host.previous.exists(), "the puller never writes a previous record"


# --- a released revision --------------------------------------------------------------


def test_a_released_revision_pulls_every_digest_the_marker_names_and_records_it(
    host, tree_source_hash
):
    result, calls = host.run()

    assert result.returncode == 0, result.stderr
    for name in CHAIN:
        reference = REAL_RECORD["images"][name]["reference"]
        assert f"pull {reference}" in calls, f"{name} was not pulled by digest: {calls}"
        assert f"tag {reference} codegen-orchestrator/{name}:{RELEASED_SHA}" in calls
    written = json.loads(host.record.read_text())
    assert written == released_record(tree_source_hash), "the record is the verified release"


def test_only_the_marker_is_resolved_and_the_token_is_read_only(host):
    result, calls = host.run()

    assert result.returncode == 0, result.stderr
    resolutions = [call for call in calls if call.startswith("buildx ")]
    assert resolutions == [
        f"buildx imagetools inspect ghcr.io/{OWNER}/codegen-orchestrator/service-release:"
        f"{RELEASED_SHA} --format {{{{.Manifest.Digest}}}}"
    ]
    curl_calls = host.curl_log.read_text()
    assert f"scope=repository:{OWNER}/codegen-orchestrator/service-release:pull " in curl_calls
    assert "pull,push" not in curl_calls


def test_validation_only_reads_the_marker_without_pulling_images_or_recording(host):
    result, calls = host.run(RELEASE_VALIDATION_ONLY="true", DIGEST_FILE=None)

    assert result.returncode == 0, result.stderr
    assert [call for call in calls if call.startswith("pull ")] == [
        f"pull ghcr.io/{OWNER}/codegen-orchestrator/service-release@sha256:service-release"
    ]
    _nothing_moved(host, calls)


# --- absent, and not known -------------------------------------------------------------


def test_a_revision_with_no_release_marker_is_refused_before_anything_is_pulled(host):
    result, calls = host.run(FAKE_MARKER_HTTP_STATUS="404")

    assert result.returncode == EXIT_NO_RELEASE, result.stderr
    assert "has no release marker" in result.stderr
    assert not [call for call in calls if call.startswith("pull ")]
    _nothing_moved(host, calls)


@pytest.mark.parametrize(
    "overrides",
    [
        {"FAKE_MARKER_HTTP_STATUS": "401"},
        {"FAKE_MARKER_HTTP_STATUS": "403"},
        {"FAKE_MARKER_HTTP_STATUS": "429"},
        {"FAKE_MARKER_HTTP_STATUS": "500"},
        {"FAKE_TOKEN_HTTP_STATUS": "401"},
        {"FAKE_TOKEN_HTTP_STATUS": "404"},
        {"FAKE_MARKER_CURL_EXIT": "6"},
        {"FAKE_MARKER_UNRESOLVED": "1"},
    ],
)
def test_a_registry_that_cannot_answer_is_a_lookup_error_never_an_absence(host, overrides):
    result, calls = host.run(**overrides)

    assert result.returncode == EXIT_MARKER_LOOKUP, result.stderr
    assert "cannot say whether" in result.stderr
    assert not [call for call in calls if call.startswith("pull ")]
    _nothing_moved(host, calls)


# --- a broken record -------------------------------------------------------------------


def _corrupt(source_hash: str, corruption: str) -> str:
    record = released_record(source_hash)
    if corruption == "undecodable":
        return "not base64 at all!"
    if corruption == "another_sha":
        record["git_sha"] = OTHER_SHA
    elif corruption == "another_source_hash":
        record["source_hash"] = "0000000000000000"
    elif corruption == "another_schema":
        record["schema_version"] = 2
    elif corruption == "no_schema":
        del record["schema_version"]
    elif corruption == "missing_image":
        del record["images"]["user-dashboard"]
    elif corruption == "extra_image":
        record["images"]["stray"] = dict(record["images"]["api"])
    elif corruption == "by_tag":
        record["images"]["api"]["reference"] = (
            f"ghcr.io/{OWNER}/codegen-orchestrator/api:{RELEASED_SHA}"
        )
    elif corruption == "other_registry":
        record["images"]["api"]["reference"] = "docker.io/library/api@sha256:abc"
    elif corruption == "self_contradiction":
        record["images"]["api"]["digest"] = "sha256:other"
    return payload(record)


@pytest.mark.parametrize(
    "corruption",
    [
        "undecodable",
        "another_sha",
        "another_source_hash",
        "another_schema",
        "no_schema",
        "missing_image",
        "extra_image",
        "by_tag",
        "other_registry",
        "self_contradiction",
    ],
)
@pytest.mark.parametrize("validation_only", [False, True])
def test_a_broken_record_is_refused_before_any_image_is_pulled(
    host, tree_source_hash, corruption, validation_only
):
    overrides = {"FAKE_MARKER": _corrupt(tree_source_hash, corruption)}
    if validation_only:
        overrides.update(RELEASE_VALIDATION_ONLY="true", DIGEST_FILE=None)

    result, calls = host.run(**overrides)

    assert result.returncode == EXIT_BROKEN_RELEASE, result.stderr
    assert not _image_pulls(calls)
    _nothing_moved(host, calls)


def test_a_release_naming_an_image_that_is_gone_is_refused(host):
    result, calls = host.run(FAKE_UNPULLABLE_IMAGE="scheduler")

    assert result.returncode == EXIT_BROKEN_RELEASE, result.stderr
    assert "scheduler" in result.stderr
    _nothing_moved(host, calls)


@pytest.mark.parametrize("label", ["deadbeefdeadbeef", "", "<no value>"])
def test_an_image_with_a_wrong_or_empty_source_hash_is_refused(host, label):
    result, calls = host.run(FAKE_ODD_IMAGE="langgraph", FAKE_ODD_LABEL=label)

    assert result.returncode == EXIT_RELEASED_LABEL, result.stderr
    assert "langgraph" in result.stderr
    _nothing_moved(host, calls)


@pytest.mark.parametrize("tag", ["latest", RELEASED_SHA[:12], ""])
def test_only_a_full_sha_is_a_revision(host, tag):
    result, calls = host.run(SERVICE_IMAGE_TAG=tag)

    assert result.returncode == EXIT_USAGE, result.stderr
    assert calls == []


@pytest.mark.parametrize("missing", ["DIGEST_FILE", "GHCR_TOKEN"])
def test_a_deploy_names_where_the_records_go(host, missing):
    result, calls = host.run(**{missing: None})

    assert result.returncode == EXIT_USAGE, result.stderr
    assert missing in result.stderr
    assert calls == []


def test_a_release_that_cannot_be_named_locally_is_a_record_failure(host):
    result, _calls = host.run(FAKE_UNTAGGABLE_IMAGE="api")

    assert result.returncode == EXIT_RECORD, result.stderr


# --- the record, and nothing else --------------------------------------------------------
#
# Rotating the live record into the previous one is not the puller's: the deploy points
# DIGEST_FILE into its pending set, and only its Switch rotates and promotes, after `up`
# (scripts/release_switch.py, scripts/tests/test_release_switch.py).


def _other_release(source_hash: str) -> str:
    record = released_record(source_hash)
    record["git_sha"] = OTHER_SHA
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def test_the_puller_replaces_the_record_it_is_given_and_rotates_nothing(host, tree_source_hash):
    host.record.write_text(_other_release(tree_source_hash))

    result, _calls = host.run()

    assert result.returncode == 0, result.stderr
    assert json.loads(host.record.read_text())["git_sha"] == RELEASED_SHA
    assert not host.previous.exists()
    assert sorted(path.name for path in host.root.glob("*.json*")) == [host.record.name]


def test_a_refusal_leaves_the_record_as_it_was(host, tree_source_hash):
    before = _other_release(tree_source_hash)
    host.record.write_text(before)

    result, calls = host.run(FAKE_ODD_IMAGE="api", FAKE_ODD_LABEL="deadbeefdeadbeef")

    assert result.returncode == EXIT_RELEASED_LABEL, result.stderr
    _nothing_moved(host, calls, record_before=before)
