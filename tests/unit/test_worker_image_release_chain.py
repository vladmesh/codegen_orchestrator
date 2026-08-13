"""The worker base images are one release chain, and the deploy checks it before it acts.

Two halves are asserted here over the workflow files themselves, because both are
about order and neither can be observed by mocking an SSH connection:

* CI publishes the whole chain for a commit under that commit's SHA, and records what
  it published.
* The production deploy pulls and verifies that release *before* the step that
  changes what is running. A deploy that discovers an incompatible worker image after
  `compose up -d` has already replaced production is the failure of GitHub #278.
"""

import json
from pathlib import Path
import subprocess

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SCRIPTS = REPO_ROOT / "infra" / "scripts"
CHAIN = ("worker-base-common", "worker-base-claude", "worker-base-factory", "worker-base-codex")
DEPLOY_SHA = "${{ github.sha }}"


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


def test_deploy_records_the_revision_and_the_verified_digests():
    steps = _deploy_steps()
    record = _index_of(steps, "record-worker-image-digests.sh")
    script = steps[record][1]

    assert record < _index_of(steps, "up -d")
    assert "GITHUB_STEP_SUMMARY" in script
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


# A fake docker for the record half: it answers a digest per image and swallows the
# login. Nothing here reaches a registry or a daemon.
FAKE_DOCKER = """#!/usr/bin/env bash
set -uo pipefail
case "$1" in
    login)
        cat > /dev/null
        ;;
    buildx)
        reference="$4"
        image="${reference##*/}"
        echo "sha256:${image%%:*}"
        ;;
    *)
        echo "fake docker: unexpected command $1" >&2
        exit 99
        ;;
esac
"""


def test_the_release_record_is_machine_readable_and_keyed_by_image(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake_docker = binaries / "docker"
    fake_docker.write_text(FAKE_DOCKER)
    fake_docker.chmod(0o755)
    record = tmp_path / "worker-images.json"

    result = subprocess.run(
        ["bash", str(SCRIPTS / "record-worker-image-digests.sh")],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{binaries}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GHCR_TOKEN": "test-token",
            "GHCR_OWNER": "test-owner",
            "GIT_SHA": "0123456789abcdef0123456789abcdef01234567",
            "SOURCE_HASH": "330fc00098074945",
            "DIGEST_FILE": str(record),
        },
    )

    assert result.returncode == 0, result.stderr
    written = json.loads(record.read_text())
    assert written["git_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert written["source_hash"] == "330fc00098074945"
    assert set(written["images"]) == set(CHAIN)
    for image in CHAIN:
        entry = written["images"][image]
        assert entry["digest"] == f"sha256:{image}"
        assert entry["reference"].endswith(f"@sha256:{image}")
        assert entry["repository"].endswith(f"/{image}:0123456789abcdef0123456789abcdef01234567")
