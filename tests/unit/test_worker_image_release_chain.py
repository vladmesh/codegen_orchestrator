"""The worker base images are one release chain, and the deploy checks it before it acts.

Two halves are asserted here over the workflow files themselves, because both are
about order and neither can be observed by mocking an SSH connection:

* CI publishes the whole chain for a commit under that commit's SHA, and records what
  it published.
* The production deploy pulls and verifies that release *before* the step that
  changes what is running. A deploy that discovers an incompatible worker image after
  `compose up -d` has already replaced production is the failure of GitHub #278.

The release protocol itself is then exercised for real, both halves against one fake
docker and a directory standing in for the registry. What is under test is the one
invariant the protocol exists for: **a partial publish must never become a release,
and no consumer may ever act on one.** Four tag pushes cannot be one registry
transaction, so the release is a fifth object — the release marker — written last and
carrying the digests of the four. Bytes left behind by a run that died between pushes
are residue, not a release: the puller refuses that revision, and a rerun of the
publish job completes it with nobody deleting anything by hand.
"""

import base64
import json
from pathlib import Path
import subprocess

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BACKEND_INTEGRATION_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "backend-integration.yml"
SCRIPTS = REPO_ROOT / "infra" / "scripts"
CHAIN = ("worker-base-common", "worker-base-claude", "worker-base-factory", "worker-base-codex")
MARKER_IMAGE = "worker-base-release"
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

    condition = " ".join(job["if"].split())
    assert job["needs"] == "merge-gate", "only a green main is published"
    assert "needs.merge-gate.result == 'success'" in condition, "only a green main is published"
    assert "github.event_name == 'push'" in condition
    assert "github.ref == 'refs/heads/main'" in condition
    # Without this the job inherits the skip of any conditional suite above the gate,
    # and a main commit that touches no frontend gets no worker release at all.
    assert condition.startswith("always()"), "a skipped ancestor must not skip the release"
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


def test_backend_dind_is_a_required_predecessor_of_the_worker_release_marker():
    """A failed DinD suite must make the same CI DAG refuse publication.

    A separate push workflow can fail after (or beside) a green CI run, so it
    cannot be a release precondition. The release gate must see this job's
    result directly, and only a green main may treat it as satisfied.
    """
    workflow = yaml.safe_load(CI_WORKFLOW.read_text())
    jobs = workflow["jobs"]
    backend = jobs["test-backend-dind-integration"]
    merge_gate = jobs["merge-gate"]
    publish = jobs["publish-worker-images"]

    assert not BACKEND_INTEGRATION_WORKFLOW.exists(), (
        "the required backend DinD suite cannot live in a parallel workflow"
    )
    assert backend["needs"] == ["fast-checks", "ci-contract"]
    assert "github.event_name == 'push'" in backend["if"]
    assert "github.event_name == 'workflow_dispatch'" in backend["if"]
    assert "github.ref == 'refs/heads/main'" in backend["if"]
    assert not backend.get("continue-on-error")

    assert "test-backend-dind-integration" in merge_gate["needs"]
    required_results = next(
        step["run"] for step in merge_gate["steps"] if step["name"] == "Check required jobs"
    )
    assert (
        '["test-backend-dind-integration"]="${{ needs.test-backend-dind-integration.result }}"'
        in required_results
    )
    assert (
        '"$job" = "test-backend-dind-integration" ] && [ "${GITHUB_REF}" != "refs/heads/main"'
    ) in required_results
    assert publish["needs"] == "merge-gate"


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
# image holding the digest its tag resolves to, and one `<image>.label` file holding
# the release record a marker was built with. `push` writes the digest file (and fails
# for FAKE_FAILING_PUSH, which is how a run that dies mid-chain is injected), `build`
# writes the label file, `buildx imagetools inspect` reads the digest file and fails
# when it is absent, exactly as a registry answers for a tag nothing pushed. Both
# halves of the chain run against this, so the registry state one leaves is the state
# the other finds. Nothing here reaches a daemon or a network.
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
            echo "ERROR: $3: manifest unknown" >&2
            exit 1
        fi
        cat "${published}"
        ;;
    build)
        sed -n 's/^LABEL [^=]*="\\(.*\\)"$/\\1/p' "$3/Dockerfile" \
            > "${FAKE_REGISTRY}/$(image_of "$2").label"
        ;;
    push)
        name="$(image_of "$1")"
        if [ "${name}" = "${FAKE_FAILING_PUSH:-}" ]; then
            echo "ERROR: $1: upload failed" >&2
            exit 1
        fi
        echo "sha256:${name}" > "${FAKE_REGISTRY}/${name}"
        ;;
    pull)
        if [ "$(image_of "$1")" = "${FAKE_UNPULLABLE_IMAGE:-}" ]; then
            echo "ERROR: $1: manifest unknown" >&2
            exit 1
        fi
        ;;
    inspect)
        name="$(image_of "$1")"
        if [[ "$*" == *worker_release* ]]; then
            cat "${FAKE_REGISTRY}/${name}.label"
        elif [ "${name}" = "${FAKE_ODD_IMAGE:-}" ]; then
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

# The build itself is not what these tests are about; they need to see whether it ran.
FAKE_MAKE = """#!/usr/bin/env bash
echo "make $*" >> "${FAKE_DOCKER_LOG}"
"""

FAKE_CURL = """#!/usr/bin/env bash
set -uo pipefail

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
elif [ -f "${FAKE_REGISTRY}/worker-base-release" ]; then
    : > "${output}"
    printf '200'
else
    : > "${output}"
    printf '404'
fi
"""

PUBLISHED_SHA = "0123456789abcdef0123456789abcdef01234567"
REGISTRY = "ghcr.io/test-owner/codegen-orchestrator"

EXIT_RELEASED_LABEL = 7
EXIT_PUBLISH_BROKEN_RELEASE = 10
EXIT_PULL_NO_RELEASE = 9


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


class ReleaseChain:
    """Both halves of the chain, run for real against one fake registry directory."""

    def __init__(self, tmp_path: Path, source_hash: str) -> None:
        self.root = tmp_path
        self.source_hash = source_hash
        binaries = tmp_path / "bin"
        binaries.mkdir()
        for name, body in (("curl", FAKE_CURL), ("docker", FAKE_DOCKER), ("make", FAKE_MAKE)):
            executable = binaries / name
            executable.write_text(body)
            executable.chmod(0o755)
        self.binaries = binaries
        self.registry = tmp_path / "registry"
        self.registry.mkdir()
        self.log = tmp_path / "docker.log"
        self.published_record = tmp_path / "worker-images.json"
        self.deployed_record = tmp_path / "deployed-worker-images.json"

    def _run(self, script: str, environment: dict) -> tuple[subprocess.CompletedProcess, list[str]]:
        self.log.write_text("")
        base = {
            "PATH": f"{self.binaries}:/usr/bin:/bin",
            "HOME": str(self.root),
            "FAKE_DOCKER_LOG": str(self.log),
            "FAKE_REGISTRY": str(self.registry),
            "FAKE_LABEL_DEFAULT": self.source_hash,
            "GHCR_TOKEN": "test-token",
            "GHCR_OWNER": "test-owner",
        }
        base.update({key: value for key, value in environment.items() if value is not None})
        result = subprocess.run(
            ["bash", str(SCRIPTS / script)],
            capture_output=True,
            text=True,
            env=base,
            cwd=self.root,
        )
        return result, self.log.read_text().splitlines()

    def publish(self, **overrides):
        environment = {"GIT_SHA": PUBLISHED_SHA, "DIGEST_FILE": str(self.published_record)}
        environment.update(overrides)
        return self._run("publish-worker-images.sh", environment)

    def pull(self, **overrides):
        environment = {
            "WORKER_IMAGE_TAG": PUBLISHED_SHA,
            "DIGEST_FILE": str(self.deployed_record),
        }
        environment.update(overrides)
        return self._run("pull-worker-images.sh", environment)

    def resolves(self, image: str) -> bool:
        """Whether the registry answers for that image's tag, as a real one would."""
        return (self.registry / image).exists()

    def release_marker_record(self) -> dict:
        """What the published marker says the release is."""
        payload = (self.registry / f"{MARKER_IMAGE}.label").read_text().strip()
        return json.loads(base64.b64decode(payload))

    def seed_release(self) -> None:
        """A SHA already released: the four images, and the marker that commits them."""
        images = {}
        for image in CHAIN:
            (self.registry / image).write_text(f"sha256:{image}\n")
            images[image] = {
                "reference": f"{REGISTRY}/{image}@sha256:{image}",
                "repository": f"{REGISTRY}/{image}",
                "digest": f"sha256:{image}",
            }
        record = {
            "git_sha": PUBLISHED_SHA,
            "source_hash": self.source_hash,
            "images": images,
        }
        (self.registry / MARKER_IMAGE).write_text(f"sha256:{MARKER_IMAGE}\n")
        (self.registry / f"{MARKER_IMAGE}.label").write_text(
            base64.b64encode(json.dumps(record).encode()).decode()
        )


@pytest.fixture
def chain(tmp_path, tree_source_hash) -> ReleaseChain:
    return ReleaseChain(tmp_path, tree_source_hash)


def _pushes(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("push ")]


def test_an_unreleased_sha_is_built_pushed_and_committed_by_the_marker_last(chain):
    result, calls = chain.publish()

    assert result.returncode == 0, result.stderr
    assert any(call.startswith("make ") for call in calls), "the chain has to be built"
    pushes = _pushes(calls)
    for image in CHAIN:
        assert any(f"/{image}:{PUBLISHED_SHA}" in push for push in pushes)
    assert f"/{MARKER_IMAGE}:{PUBLISHED_SHA}" in pushes[-1], (
        f"the marker is the commit point and must be written last: {pushes}"
    )

    written = json.loads(chain.published_record.read_text())
    assert written["git_sha"] == PUBLISHED_SHA
    assert set(written["images"]) == set(CHAIN)
    for image in CHAIN:
        entry = written["images"][image]
        assert entry["digest"] == f"sha256:{image}"
        assert entry["reference"] == f"{entry['repository']}@{entry['digest']}"
        assert entry["repository"].endswith(f"/{image}")

    assert chain.release_marker_record() == written, (
        "the release marker carries the digest record of exactly what was published"
    )


def test_a_rerun_of_a_released_sha_pushes_nothing_and_records_the_same_release(
    chain, tree_source_hash
):
    """A released SHA is frozen: a rerun re-verifies it from the marker, it does not rewrite it."""
    chain.seed_release()
    result, calls = chain.publish()

    assert result.returncode == 0, result.stderr
    assert not _pushes(calls), f"an already-released SHA must not be pushed over: {calls}"
    assert not [call for call in calls if call.startswith("make ")], (
        "an already-released SHA does not even need to be rebuilt"
    )
    written = json.loads(chain.published_record.read_text())
    assert written["source_hash"] == tree_source_hash
    assert set(written["images"]) == set(CHAIN)


def test_a_push_that_fails_mid_chain_releases_nothing_and_the_deploy_refuses_that_sha(chain):
    """The failure the whole protocol exists for, injected after a successful push.

    The first image lands in the registry and the second push fails. What must hold is
    not that the registry is clean — it is not, and no shell can make four pushes one
    transaction — but that nothing claims to be a release, and that the consumer acts
    on nothing.
    """
    result, calls = chain.publish(FAKE_FAILING_PUSH="worker-base-claude")

    assert result.returncode != 0
    assert chain.resolves("worker-base-common"), "the first push landed; this is the residue"
    assert not chain.resolves(MARKER_IMAGE), (
        "a run that died mid-chain must not have committed a release"
    )
    assert not chain.published_record.exists(), "nothing may be recorded as published"
    assert not any(call.startswith("build ") for call in calls), (
        "the marker is never built once a push has failed"
    )

    deploy, deploy_calls = chain.pull()

    assert deploy.returncode == EXIT_PULL_NO_RELEASE, deploy.stderr
    assert MARKER_IMAGE in deploy.stderr, "the deploy has to name the absent release marker"
    assert not [call for call in deploy_calls if call.startswith("tag ")], (
        "residue must not move a single local worker-base-*:latest name"
    )
    assert not chain.deployed_record.exists()


def test_a_retry_after_a_failed_push_completes_that_sha_with_no_hand_in_the_registry(chain):
    """Rule 2: with no marker the SHA is not released, so a rerun may finish it."""
    chain.publish(FAKE_FAILING_PUSH="worker-base-claude")

    result, calls = chain.publish()

    assert result.returncode == 0, result.stderr
    assert _pushes(calls), "the retry re-pushes over its own residue"
    assert chain.resolves(MARKER_IMAGE), "the retry commits the release it could not commit before"

    deploy, deploy_calls = chain.pull()

    assert deploy.returncode == 0, deploy.stderr
    for image in CHAIN:
        assert any(
            call.startswith("tag ") and call.endswith(f"{image}:latest") for call in deploy_calls
        ), f"{image} was not retagged for worker-manager: {deploy_calls}"
    assert json.loads(chain.deployed_record.read_text()) == chain.release_marker_record(), (
        "the deploy records exactly the release the marker committed"
    )


def test_a_released_sha_with_a_stale_label_is_refused_not_overwritten(chain):
    chain.seed_release()
    result, calls = chain.publish(
        FAKE_ODD_IMAGE="worker-base-codex", FAKE_ODD_LABEL="dead0000dead0000"
    )

    assert result.returncode == EXIT_RELEASED_LABEL, result.stderr
    assert "worker-base-codex" in result.stderr
    assert "dead0000dead0000" in result.stderr
    assert not _pushes(calls)


def test_a_released_sha_whose_image_is_gone_is_refused_not_repaired(chain):
    """Rule 3: corruption of a committed release is a decision, not a retry."""
    chain.seed_release()
    result, calls = chain.publish(FAKE_UNPULLABLE_IMAGE="worker-base-factory")

    assert result.returncode == EXIT_PUBLISH_BROKEN_RELEASE, result.stderr
    assert "worker-base-factory" in result.stderr
    assert not _pushes(calls)
    assert not [call for call in calls if call.startswith("make ")], (
        "a committed release is not rebuilt over"
    )
