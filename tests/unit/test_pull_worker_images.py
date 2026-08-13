"""The deploy refuses worker images that were not built from the revision it deploys.

`infra/scripts/pull-worker-images.sh` is the consuming half of the worker base image
release chain. These tests run it for real against a fake `docker` on PATH, so they
need neither a registry nor a docker daemon: what is exercised is the verification
path itself — which images it pulls, what it does with the source hash label it finds,
and whether it moves a local tag before it has verified everything.

Each refusal has its own exit code, because the deploy has to be able to say which
failure it hit: a SHA that was never published is a different problem from an image
that was published without a label, which is different again from an image built from
other sources.
"""

import json
from pathlib import Path
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PULL_SCRIPT = REPO_ROOT / "infra" / "scripts" / "pull-worker-images.sh"
CHAIN = ("worker-base-common", "worker-base-claude", "worker-base-factory", "worker-base-codex")
DEPLOYED_SHA = "0123456789abcdef0123456789abcdef01234567"

EXIT_USAGE = 1
EXIT_MISSING_IMAGE = 3
EXIT_MISSING_LABEL = 4
EXIT_STALE_LABEL = 5

# A fake docker. `buildx imagetools inspect` answers a digest per image and fails for
# FAKE_MISSING_IMAGE the way a registry answers for an unpublished tag; `inspect`
# answers the label FAKE_LABEL_DEFAULT unless the image is FAKE_ODD_IMAGE, which
# answers FAKE_ODD_LABEL. Every call is appended to FAKE_DOCKER_LOG so a test can see
# what the script did.
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
        if [ "$(image_of "$3")" = "${FAKE_MISSING_IMAGE:-}" ]; then
            echo "ERROR: $3: not found" >&2
            exit 1
        fi
        echo "sha256:$(image_of "$3")"
        ;;
    pull)
        ;;
    inspect)
        if [ "$(image_of "$1")" = "${FAKE_ODD_IMAGE:-}" ]; then
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


@pytest.fixture
def run_pull(tmp_path, tree_source_hash):
    """Run the pull script with a fake docker, returning the result and its calls."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake_docker = binaries / "docker"
    fake_docker.write_text(FAKE_DOCKER)
    fake_docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()
    record = tmp_path / "deployed-worker-images.json"

    def run(**overrides):
        environment = {
            "PATH": f"{binaries}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_LABEL_DEFAULT": tree_source_hash,
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


def test_the_release_is_pulled_verified_and_recorded_as_one_resolved_digest(run_pull):
    """The tag is resolved once; everything after that names the digest it resolved to.

    A second lookup of the same tag can answer differently, and then what the deploy
    records is not evidence about the images it verified. So the pull, the label check,
    the local retag and the record all have to name the digest, not the tag.
    """
    result, calls, record = run_pull()

    assert result.returncode == 0, result.stderr
    written = json.loads(record.read_text())
    assert written["git_sha"] == DEPLOYED_SHA
    assert set(written["images"]) == set(CHAIN)

    for image in CHAIN:
        entry = written["images"][image]
        assert entry["digest"] == f"sha256:{image}"
        assert entry["reference"] == f"{entry['repository']}@{entry['digest']}"

        resolutions = [
            call for call in calls if call.startswith("buildx ") and f"/{image}:" in call
        ]
        assert len(resolutions) == 1, f"{image} was resolved more than once: {resolutions}"
        for verb in ("pull", "inspect", "tag"):
            assert any(
                call.startswith(f"{verb} ") and call.split()[1].endswith(entry["reference"])
                for call in calls
            ), f"{verb} did not name the resolved digest of {image}: {calls}"


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


def test_an_unpublished_image_is_refused(run_pull):
    result, calls, record = run_pull(FAKE_MISSING_IMAGE="worker-base-factory")

    assert result.returncode == EXIT_MISSING_IMAGE, result.stderr
    assert "worker-base-factory" in result.stderr
    assert not [call for call in calls if call.startswith("tag ")]
    assert not [
        call for call in calls if call.startswith("pull ") and "worker-base-factory" in call
    ], "a tag that does not resolve is never pulled"
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
