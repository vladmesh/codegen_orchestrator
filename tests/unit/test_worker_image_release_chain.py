"""The worker base images are one release chain, and the deploy checks it before it acts.

Two halves are asserted here over the workflow files themselves, because both are
about order and neither can be observed by mocking an SSH connection:

* CI publishes the whole chain for a commit under that commit's SHA, and records what
  it published.
* The production deploy pulls and verifies that release *before* the step that
  changes what is running. A deploy that discovers an incompatible worker image after
  `compose up -d` has already replaced production is the failure of GitHub #278.

The publish protocol itself is exercised for real against a fake docker and a
directory standing in for the registry, because the release is only immutable if the
script refuses the states a retry can find: a SHA already published whole (rerun, push
nothing) and a SHA published in part (refuse, name the tags).
"""

import json
from pathlib import Path
import subprocess

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SCRIPTS = REPO_ROOT / "infra" / "scripts"
CHAIN = ("worker-base-common", "worker-base-claude", "worker-base-factory", "worker-base-codex")
DEPLOY_SHA = "${{ github.sha }}"
# Where the verification writes down what it verified, on the deployment host.
HOST_RECORD = "${{ env.DEPLOY_PATH }}/deployed-worker-images.json"


def _deploy_steps() -> list[tuple[str, str]]:
    """(name, script) of every deploy step, in the order the job runs them."""
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text())
    steps = []
    for step in workflow["jobs"]["deploy"]["steps"]:
        script = step.get("run") or step.get("with", {}).get("script") or ""
        steps.append((step["name"], script))
    return steps


def _index_of(steps: list[tuple[str, str]], needle: str) -> int:
    matches = [index for index, (_name, script) in enumerate(steps) if needle in script]
    assert len(matches) == 1, f"expected exactly one step containing {needle!r}, got {matches}"
    return matches[0]


def test_deploy_verifies_worker_images_before_it_changes_what_is_running():
    steps = _deploy_steps()
    verification = _index_of(steps, "pull-worker-images.sh")
    mutation = _index_of(steps, "up -d")

    assert verification < mutation, (
        f"worker images are verified in step {verification} "
        f"({steps[verification][0]}) but production is replaced in step {mutation} "
        f"({steps[mutation][0]}); an incompatible image must fail before compose up -d"
    )


def test_deploy_pulls_the_exact_revision_it_deploys():
    steps = _deploy_steps()
    script = steps[_index_of(steps, "pull-worker-images.sh")][1]

    assert f"WORKER_IMAGE_TAG='{DEPLOY_SHA}'" in script


def test_deploy_records_the_digests_it_verified_instead_of_resolving_them_again():
    """The recorded release has to be the one that was checked, not another lookup.

    Resolving the same tag a second time can answer with a different digest, and then
    the deploy's record is not evidence about the images it verified. The verification
    writes the record on the host; this step only carries that file back.
    """
    steps = _deploy_steps()
    pull = _index_of(steps, "pull-worker-images.sh")
    record = _index_of(steps, "GITHUB_STEP_SUMMARY")
    script = steps[record][1]

    assert f"DIGEST_FILE='{HOST_RECORD}'" in steps[pull][1]
    assert pull < record < _index_of(steps, "up -d")
    assert HOST_RECORD in script
    assert "imagetools" not in script, "the record must not be a second resolution"

    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text())
    uploads = [
        step
        for step in workflow["jobs"]["deploy"]["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert uploads, "the deploy must keep the digests it deployed as a run artifact"
    assert all(step["with"]["if-no-files-found"] == "error" for step in uploads)


def test_ci_publishes_the_whole_chain_for_the_commit_it_builds():
    workflow = yaml.safe_load(CI_WORKFLOW.read_text())
    job = workflow["jobs"]["publish-worker-images"]

    assert job["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert job["needs"] == "merge-gate", "only a green main is published"
    assert job["permissions"]["packages"] == "write"

    publish = next(
        step for step in job["steps"] if "publish-worker-images.sh" in step.get("run", "")
    )
    assert publish["env"]["GIT_SHA"] == DEPLOY_SHA, "the tag is the SHA being built"

    upload = next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    )
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["path"] == publish["env"]["DIGEST_FILE"]


def test_the_chain_is_listed_once_and_both_halves_read_it():
    listed = (SCRIPTS / "worker-images.sh").read_text()
    for image in CHAIN:
        assert f'"{image}"' in listed

    for half in ("publish-worker-images.sh", "pull-worker-images.sh"):
        script = (SCRIPTS / half).read_text()
        assert "worker-images.sh" in script, f"{half} must read the chain, not repeat it"
        for image in CHAIN:
            assert f'"{image}"' not in script


def test_publish_reuses_the_makefile_chain_and_publishes_no_mutable_tag():
    script = (SCRIPTS / "publish-worker-images.sh").read_text()

    assert "make -C" in script and "rebuild-worker-images" in script
    assert ':latest"' not in script.split("docker push")[1], (
        "a mutable :latest must never be pushed to the registry"
    )


# A fake docker with a directory standing in for the registry: one file per published
# image, holding the digest that image's tag resolves to. `push` writes such a file,
# `buildx imagetools inspect` reads it and fails when it is absent, exactly as a
# registry answers for an unpublished tag. Nothing here reaches a daemon or a network.
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
        published="${FAKE_REGISTRY}/$(image_of "$3")"
        if [ ! -f "${published}" ]; then
            echo "ERROR: $3: not found" >&2
            exit 1
        fi
        cat "${published}"
        ;;
    push)
        echo "sha256:$(image_of "$1")" > "${FAKE_REGISTRY}/$(image_of "$1")"
        ;;
    inspect)
        if [ "$(image_of "$1")" = "${FAKE_ODD_IMAGE:-}" ]; then
            echo "${FAKE_ODD_LABEL}"
        else
            echo "${FAKE_LABEL_DEFAULT}"
        fi
        ;;
    pull|tag)
        ;;
    *)
        echo "fake docker: unexpected command ${command}" >&2
        exit 99
        ;;
esac
"""

# The build itself is not what these tests are about; they need to see whether it ran.
FAKE_MAKE = """#!/usr/bin/env bash
echo "make $*" >> "${FAKE_DOCKER_LOG}"
"""

PUBLISHED_SHA = "0123456789abcdef0123456789abcdef01234567"
EXIT_PARTIAL_RELEASE = 6
EXIT_PUBLISHED_LABEL = 7


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
def run_publish(tmp_path, tree_source_hash):
    """Run the publish script against a fake docker and a fake registry directory."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("make", FAKE_MAKE)):
        executable = binaries / name
        executable.write_text(body)
        executable.chmod(0o755)
    registry = tmp_path / "registry"
    registry.mkdir()
    log = tmp_path / "docker.log"
    log.touch()
    record = tmp_path / "worker-images.json"

    def run(already_published: tuple[str, ...] = (), **overrides):
        for image in already_published:
            (registry / image).write_text(f"sha256:{image}\n")
        environment = {
            "PATH": f"{binaries}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_REGISTRY": str(registry),
            "FAKE_LABEL_DEFAULT": tree_source_hash,
            "GHCR_TOKEN": "test-token",
            "GHCR_OWNER": "test-owner",
            "GIT_SHA": PUBLISHED_SHA,
            "DIGEST_FILE": str(record),
        }
        environment.update(overrides)
        result = subprocess.run(
            ["bash", str(SCRIPTS / "publish-worker-images.sh")],
            capture_output=True,
            text=True,
            env=environment,
            cwd=tmp_path,
        )
        return result, log.read_text().splitlines(), record

    return run


def test_an_unpublished_sha_is_built_pushed_and_recorded(run_publish):
    result, calls, record = run_publish()

    assert result.returncode == 0, result.stderr
    assert any(call.startswith("make ") for call in calls), "the chain has to be built"
    for image in CHAIN:
        assert any(
            call.startswith("push ") and f"/{image}:{PUBLISHED_SHA}" in call for call in calls
        )

    written = json.loads(record.read_text())
    assert written["git_sha"] == PUBLISHED_SHA
    assert set(written["images"]) == set(CHAIN)
    for image in CHAIN:
        entry = written["images"][image]
        assert entry["digest"] == f"sha256:{image}"
        assert entry["reference"] == f"{entry['repository']}@{entry['digest']}"
        assert entry["repository"].endswith(f"/{image}")


def test_a_rerun_of_a_published_sha_pushes_nothing_and_records_the_same_release(
    run_publish, tree_source_hash
):
    """A published SHA is a written release: a rerun re-verifies it, it does not rewrite it."""
    result, calls, record = run_publish(already_published=CHAIN)

    assert result.returncode == 0, result.stderr
    assert not [call for call in calls if call.startswith("push ")], (
        f"an already-published SHA must not be pushed over: {calls}"
    )
    assert not [call for call in calls if call.startswith("make ")], (
        "an already-published SHA does not even need to be rebuilt"
    )
    written = json.loads(record.read_text())
    assert written["source_hash"] == tree_source_hash
    assert set(written["images"]) == set(CHAIN)


def test_a_half_published_sha_is_refused_naming_what_exists_and_what_is_missing(run_publish):
    """The state a mid-chain push failure leaves behind. Completing it is a decision."""
    result, calls, _record = run_publish(already_published=("worker-base-common",))

    assert result.returncode == EXIT_PARTIAL_RELEASE, result.stderr
    assert "worker-base-common@sha256:worker-base-common" in result.stderr
    for image in ("worker-base-claude", "worker-base-factory", "worker-base-codex"):
        assert f"{image}:{PUBLISHED_SHA}" in result.stderr
    assert not [call for call in calls if call.startswith("push ")]
    assert not [call for call in calls if call.startswith("make ")]


def test_a_published_release_with_a_stale_label_is_refused_not_overwritten(run_publish):
    result, calls, _record = run_publish(
        already_published=CHAIN,
        FAKE_ODD_IMAGE="worker-base-codex",
        FAKE_ODD_LABEL="dead0000dead0000",
    )

    assert result.returncode == EXIT_PUBLISHED_LABEL, result.stderr
    assert "worker-base-codex" in result.stderr
    assert "dead0000dead0000" in result.stderr
    assert not [call for call in calls if call.startswith("push ")]
