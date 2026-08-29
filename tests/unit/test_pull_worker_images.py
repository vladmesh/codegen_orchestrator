"""The deploy refuses worker images that were not released for the revision it deploys.

`infra/scripts/pull-worker-images.sh` is the consuming half of the worker base image
release chain, and the one place that decides whether a revision has a release at all.
These tests run it for real against a fake `docker` on PATH, so they need neither a
registry nor a docker daemon: what is exercised is the verification path itself — what
it consults, which images it pulls, what it does with the source hash label it finds,
and whether it moves a local tag before it has verified everything.

The first thing it consults is the release marker, because image tags are not a
release: a publish run cancelled between two pushes leaves tags behind that were never
released. No marker means no release, whatever tags exist.

Each refusal has its own exit code, because the deploy has to be able to say which
failure it hit: a revision that was never released is a different problem from a
release whose image is gone, which is different again from an image built from other
sources.
"""

import base64
import json
from pathlib import Path
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PULL_SCRIPT = REPO_ROOT / "infra" / "scripts" / "pull-worker-images.sh"
CHAIN = ("worker-base-common", "worker-base-claude", "worker-base-factory", "worker-base-codex")
DEPLOYED_SHA = "0123456789abcdef0123456789abcdef01234567"
REGISTRY = "ghcr.io/test-owner/codegen-orchestrator"

EXIT_USAGE = 1
EXIT_MISSING_IMAGE = 3
EXIT_MISSING_LABEL = 4
EXIT_STALE_LABEL = 5
EXIT_BROKEN_RELEASE = 6
EXIT_NO_RELEASE = 9
EXIT_REGISTRY_AUTH = 10
EXIT_REGISTRY_TRANSPORT = 11
EXIT_REGISTRY_RATE_LIMIT = 12
EXIT_REGISTRY_TOOL = 13

# A fake docker. `buildx imagetools inspect` answers a digest per image and fails for
# FAKE_MISSING_IMAGE the way a registry answers for a tag that was never pushed;
# `pull` fails for FAKE_UNPULLABLE_IMAGE, the way it answers for a reference that
# resolves but whose blobs are gone. `inspect` answers the release record FAKE_MARKER
# when it is asked for the release label, and otherwise the source hash label
# FAKE_LABEL_DEFAULT, unless the image is FAKE_ODD_IMAGE, which answers FAKE_ODD_LABEL.
# Every call is appended to FAKE_DOCKER_LOG so a test can see what the script did.
FAKE_DOCKER = """#!/usr/bin/env bash
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
        if [ "$(image_of "$3")" = "worker-base-release" ] && [ -n "${FAKE_MARKER_ERROR:-}" ]; then
            echo "${FAKE_MARKER_ERROR}" >&2
            exit 1
        fi
        if [ "$(image_of "$3")" = "${FAKE_MISSING_IMAGE:-}" ]; then
            echo "ERROR: $3: manifest unknown" >&2
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
        if [[ "$*" == *worker_release* ]]; then
            echo "${FAKE_MARKER}"
        elif [ "$(image_of "$1")" = "${FAKE_ODD_IMAGE:-}" ]; then
            echo "${FAKE_ODD_LABEL}"
        else
            echo "${FAKE_LABEL_DEFAULT}"
        fi
        ;;
    tag|images)
        ;;
    *)
        echo "fake docker: unexpected command ${command}" >&2
        exit 99
        ;;
esac
"""

FAKE_CURL = """#!/usr/bin/env bash
set -uo pipefail
echo "$*" >> "${FAKE_CURL_LOG}"

output=""
url=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --output)
            output="$2"
            shift 2
            ;;
        http*)
            url="$1"
            shift
            ;;
        *)
            shift
            ;;
    esac
done

if [[ "${url}" == *"/token"* ]]; then
    printf '{"token":"fake-registry-token"}' > "${output}"
    printf '200'
else
    : > "${output}"
    printf '%s' "${FAKE_MARKER_HTTP_STATUS:-200}"
fi
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


def release_record(source_hash: str, git_sha: str = DEPLOYED_SHA, **images) -> dict:
    """The record a publish run writes into the release marker of one revision."""
    published = {
        image: {
            "reference": f"{REGISTRY}/{image}@sha256:{image}",
            "repository": f"{REGISTRY}/{image}",
            "digest": f"sha256:{image}",
        }
        for image in CHAIN
    }
    published.update(images)
    return {"git_sha": git_sha, "source_hash": source_hash, "images": published}


def marker_payload(record: dict) -> str:
    return base64.b64encode(json.dumps(record).encode()).decode()


@pytest.fixture
def run_pull(tmp_path, tree_source_hash):
    """Run the pull script with a fake docker, returning the result and its calls."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake_docker = binaries / "docker"
    fake_docker.write_text(FAKE_DOCKER)
    fake_docker.chmod(0o755)
    fake_curl = binaries / "curl"
    fake_curl.write_text(FAKE_CURL)
    fake_curl.chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()
    curl_log = tmp_path / "curl.log"
    curl_log.touch()
    record = tmp_path / "deployed-worker-images.json"

    def run(**overrides):
        environment = {
            "PATH": f"{binaries}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_CURL_LOG": str(curl_log),
            "FAKE_LABEL_DEFAULT": tree_source_hash,
            "FAKE_MARKER": marker_payload(release_record(tree_source_hash)),
            "GHCR_TOKEN": "test-token",
            "GHCR_OWNER": "test-owner",
            "WORKER_IMAGE_TAG": DEPLOYED_SHA,
            "DIGEST_FILE": str(record),
        }
        environment.update({key: value for key, value in overrides.items() if value is not None})
        for key, value in overrides.items():
            if value is None:
                environment.pop(key, None)
        result = subprocess.run(
            ["bash", str(PULL_SCRIPT)],
            capture_output=True,
            text=True,
            env=environment,
            cwd=tmp_path,  # the script has to find its own repository, not use the cwd
        )
        return result, log.read_text().splitlines(), record

    return run


def test_matching_release_is_accepted_and_retagged(run_pull):
    result, calls, _record = run_pull()

    assert result.returncode == 0, result.stderr
    for image in CHAIN:
        assert any(call.startswith("pull ") and f"/{image}@sha256:" in call for call in calls), (
            f"{image} was not pulled: {calls}"
        )
        assert any(
            call.startswith("tag ") and call.endswith(f"{image}:latest") for call in calls
        ), f"{image} was not retagged for worker-manager: {calls}"


def test_the_release_is_read_from_the_marker_and_never_from_a_tag(run_pull):
    """The marker is the release; the images are the digests it names.

    Resolving the image tags here instead would deploy whatever a failed publish run
    left in the registry, and a second lookup of a tag can answer differently from the
    one the publisher recorded. So exactly one thing is resolved — the marker — and
    the pull, the label check, the local retag and the record all name the digests it
    carries.
    """
    result, calls, record = run_pull()

    assert result.returncode == 0, result.stderr
    resolutions = [call for call in calls if call.startswith("buildx ")]
    assert len(resolutions) == 1, f"only the release marker is resolved: {resolutions}"
    assert "worker-base-release" in resolutions[0]

    written = json.loads(record.read_text())
    assert written["git_sha"] == DEPLOYED_SHA
    assert set(written["images"]) == set(CHAIN)

    for image in CHAIN:
        entry = written["images"][image]
        assert entry["reference"] == f"{REGISTRY}/{image}@sha256:{image}"
        for verb in ("pull", "inspect", "tag"):
            assert any(
                call.startswith(f"{verb} ") and call.split()[1].endswith(entry["reference"])
                for call in calls
            ), f"{verb} did not name the digest the marker holds for {image}: {calls}"


def test_a_revision_with_no_release_marker_is_refused_before_anything_moves(run_pull):
    """The state a cancelled publish run leaves: image tags exist, the release does not.

    The fake registry here answers every image tag, exactly as it would after a run
    that pushed some images and died. Only the marker is missing, and that alone has
    to stop the deploy.
    """
    result, calls, record = run_pull(FAKE_MARKER_HTTP_STATUS="404")

    assert result.returncode == EXIT_NO_RELEASE, result.stderr
    assert "worker-base-release" in result.stderr
    assert DEPLOYED_SHA in result.stderr
    assert not [call for call in calls if call.startswith("tag ")], (
        "a revision with no release must leave the local worker-base-*:latest names alone"
    )
    assert not [call for call in calls if call.startswith("pull ")], (
        "no image is pulled for a revision that was never released"
    )
    assert not record.exists()


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [
        ("401", EXIT_REGISTRY_AUTH),
        ("403", EXIT_REGISTRY_AUTH),
        ("429", EXIT_REGISTRY_RATE_LIMIT),
        ("500", EXIT_REGISTRY_TOOL),
    ],
)
def test_only_a_typed_registry_404_admits_the_missing_release_path(run_pull, status, expected_exit):
    result, calls, record = run_pull(FAKE_MARKER_HTTP_STATUS=status)

    assert result.returncode == expected_exit, result.stderr
    assert not [call for call in calls if call.startswith("pull ")]
    assert not [call for call in calls if call.startswith("tag ")]
    assert not record.exists()


def test_ambiguous_image_tool_failure_is_not_misclassified_as_a_missing_release(run_pull):
    result, calls, record = run_pull(FAKE_MARKER_ERROR="ERROR: marker not found")

    assert result.returncode == EXIT_REGISTRY_TOOL, result.stderr
    assert not [call for call in calls if call.startswith("pull ")]
    assert not [call for call in calls if call.startswith("tag ")]
    assert not record.exists()


def test_a_release_naming_an_image_that_is_gone_is_refused(run_pull):
    """Rule 3: a committed release missing an image is corruption, not a retry."""
    result, calls, record = run_pull(FAKE_UNPULLABLE_IMAGE="worker-base-factory")

    assert result.returncode == EXIT_MISSING_IMAGE, result.stderr
    assert "worker-base-factory" in result.stderr
    assert not [call for call in calls if call.startswith("tag ")]
    assert not record.exists()


def test_a_stale_source_hash_is_refused_naming_both_hashes(run_pull, tree_source_hash):
    result, calls, record = run_pull(
        FAKE_ODD_IMAGE="worker-base-claude", FAKE_ODD_LABEL="dead0000dead0000"
    )

    assert result.returncode == EXIT_STALE_LABEL, result.stderr
    assert "worker-base-claude" in result.stderr
    assert tree_source_hash in result.stderr
    assert "dead0000dead0000" in result.stderr
    assert not [call for call in calls if call.startswith("tag ")], (
        "a refused release must leave the local worker-base-*:latest names alone"
    )
    assert not record.exists(), "a refused release is not recorded as deployed"


def test_a_missing_source_hash_label_is_refused(run_pull, tree_source_hash):
    result, calls, record = run_pull(FAKE_ODD_IMAGE="worker-base-codex", FAKE_ODD_LABEL="")

    assert result.returncode == EXIT_MISSING_LABEL, result.stderr
    assert "worker-base-codex" in result.stderr
    assert tree_source_hash in result.stderr
    assert not [call for call in calls if call.startswith("tag ")]
    assert not record.exists()


def test_a_marker_carrying_no_record_is_refused(run_pull):
    result, calls, record = run_pull(FAKE_MARKER="")

    assert result.returncode == EXIT_BROKEN_RELEASE, result.stderr
    assert not [call for call in calls if call.startswith("tag ")]
    assert not record.exists()


def test_a_marker_for_another_revision_is_refused(run_pull, tree_source_hash):
    """The marker of one SHA must not be deployable as another's."""
    other = release_record(tree_source_hash, git_sha="f" * 40)
    result, calls, record = run_pull(FAKE_MARKER=marker_payload(other))

    assert result.returncode == EXIT_BROKEN_RELEASE, result.stderr
    assert not [call for call in calls if call.startswith("tag ")]
    assert not record.exists()


def test_a_marker_naming_an_image_outside_the_registry_is_refused(run_pull, tree_source_hash):
    """A record is trusted for digests only inside the namespace it was published in."""
    elsewhere = release_record(
        tree_source_hash,
        **{
            "worker-base-codex": {
                "reference": "ghcr.io/somebody-else/worker-base-codex@sha256:worker-base-codex",
                "repository": "ghcr.io/somebody-else/worker-base-codex",
                "digest": "sha256:worker-base-codex",
            }
        },
    )
    result, calls, record = run_pull(FAKE_MARKER=marker_payload(elsewhere))

    assert result.returncode == EXIT_BROKEN_RELEASE, result.stderr
    assert not [call for call in calls if call.startswith("tag ")]
    assert not record.exists()


def test_the_tag_has_no_default(run_pull):
    result, calls, _record = run_pull(WORKER_IMAGE_TAG=None)

    assert result.returncode != 0
    assert "WORKER_IMAGE_TAG" in result.stderr
    assert calls == [], "nothing may be pulled without an explicit tag"


def test_the_mutable_latest_tag_is_refused(run_pull):
    result, calls, _record = run_pull(WORKER_IMAGE_TAG="latest")

    assert result.returncode == EXIT_USAGE, result.stderr
    assert "latest" in result.stderr
    assert calls == [], "the mutable tag must be refused before anything is pulled"


def test_the_script_declares_no_fallback_tag():
    """The regression itself: a default tag is how :latest reached production."""
    script = PULL_SCRIPT.read_text()

    assert "WORKER_IMAGE_TAG:-" not in script
    assert "WORKER_IMAGE_TAG:?" in script
    assert "Authorization: Bearer ${registry_token}" not in script
    assert '"@${AUTH_HEADER_FILE}"' in script
