"""The control-plane service images are one release per green main SHA.

The protocol is the worker chain's (tests/unit/test_worker_image_release_chain.py): image
tags are candidates, and the release of a SHA is one further object, the release marker,
written last and carrying the digest record of every image. The service chain splits the
publisher in two stages so the builds can run beside the test jobs:

* ``candidates`` builds and pushes every image under the SHA tag, before anything is
  known to be green. That is safe only because a candidate is never a release.
* ``release`` runs after the Required CI Gate, verifies every candidate's digest carries
  this tree's source hash, and writes the marker.

So the invariant under test has two halves. Over the real ``ci.yml``: **the marker can
never be written before the gate is green**. Over the script, run for real against a fake
docker and a directory standing in for the registry: **a partial or unverified publish
never becomes a release, and a committed release is never rewritten.**
"""

import base64
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SCRIPTS = REPO_ROOT / "infra" / "scripts"
LIST = SCRIPTS / "service-images.sh"
PUBLISH = SCRIPTS / "publish-service-images.sh"
COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yml"))
MARKER_IMAGE = "service-release"
RELEASE_STAGE = "publish-service-images.sh release"
CANDIDATE_STAGE = "publish-service-images.sh candidates"
PUSH_TO_MAIN = ("github.event_name == 'push'", "github.ref == 'refs/heads/main'")

PUBLISHED_SHA = "0123456789abcdef0123456789abcdef01234567"
REGISTRY = "ghcr.io/test-owner/codegen-orchestrator"
SCHEMA_VERSION = 1

EXIT_USAGE = 1
EXIT_CANDIDATE_LABEL = 2
EXIT_CANDIDATE_NO_LABEL = 3
EXIT_BUILD = 4
EXIT_RELEASED_LABEL = 7
EXIT_UNRESOLVED = 8
EXIT_BROKEN_RELEASE = 10


def _listed() -> list[tuple[str, str, str]]:
    """(image, dockerfile, context) of every entry, read the way the scripts read it."""
    result = subprocess.run(
        ["bash", "-c", f'source "{LIST}"; printf "%s\\n" "${{SERVICE_IMAGES[@]}}"'],
        capture_output=True,
        text=True,
        check=True,
    )
    entries = []
    for line in result.stdout.splitlines():
        image, dockerfile, context = line.split()
        entries.append((image, dockerfile, context))
    return entries


def _names() -> list[str]:
    return [image for image, _dockerfile, _context in _listed()]


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates compose's own tags, `!reset` in the prod override."""


_ComposeLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _compose_build_targets() -> dict[tuple[str, str], set[str]]:
    """(dockerfile, context) -> the image names production compose builds from it."""
    targets: dict[tuple[str, str], set[str]] = {}
    for path in COMPOSE_FILES:
        document = yaml.load(path.read_text(), _ComposeLoader)  # noqa: S506 - a SafeLoader
        services = (document or {}).get("services") or {}
        for service_name, service in services.items():
            build = service.get("build") if isinstance(service, dict) else None
            if build is None:
                continue
            if isinstance(build, str):
                build = {"context": build}
            context = Path(build.get("context", ".")).as_posix()
            context = "." if context in ("", "./") else context.removeprefix("./")
            dockerfile = (Path(context) / build.get("dockerfile", "Dockerfile")).as_posix()
            image = service.get("image")
            name = (
                image.removeprefix("codegen-orchestrator/").partition(":")[0]
                if isinstance(image, str)
                else service_name
            )
            targets.setdefault((dockerfile.removeprefix("./"), context), set()).add(name)
    return targets


# --- The list -------------------------------------------------------------------------


def test_every_image_is_listed_once():
    entries = _listed()
    names = [image for image, _dockerfile, _context in entries]
    dockerfiles = [dockerfile for _image, dockerfile, _context in entries]

    assert len(names) == len(set(names)), f"an image is listed twice: {names}"
    assert len(dockerfiles) == len(set(dockerfiles)), (
        f"a Dockerfile is listed twice, so its image would be published twice: {dockerfiles}"
    )


def test_every_production_dockerfile_is_listed():
    production = {
        path.relative_to(REPO_ROOT).as_posix() for path in REPO_ROOT.glob("services/*/Dockerfile")
    }
    listed = {dockerfile for _image, dockerfile, _context in _listed()}

    assert production == listed, (
        f"not listed: {sorted(production - listed)}; listed but not a service Dockerfile: "
        f"{sorted(listed - production)}"
    )


def test_every_compose_build_target_is_listed_under_its_compose_name():
    targets = _compose_build_targets()
    listed = {(dockerfile, context): image for image, dockerfile, context in _listed()}

    assert targets, "no compose file builds anything; the list would check nothing"
    assert set(targets) == set(listed), (
        f"compose builds {sorted(set(targets) - set(listed))} that is not listed, "
        f"or the list has {sorted(set(listed) - set(targets))} that compose never builds"
    )
    for target, names in targets.items():
        assert names == {listed[target]}, (
            f"{target} is built by compose as {sorted(names)} but listed as {listed[target]}; "
            "one Dockerfile is one image, published once"
        )


def test_the_import_check_covers_listed_images_only():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import check_service_image_imports as imports
    finally:
        sys.path.pop(0)
    listed = {image: dockerfile for image, dockerfile, _context in _listed()}

    for service in imports.SERVICE_IMAGES:
        assert listed.get(service.name) == service.dockerfile, service


def test_the_publisher_reads_the_list_instead_of_repeating_it():
    script = PUBLISH.read_text()

    assert 'source "${SCRIPT_DIR}/service-images.sh"' in script
    for image, dockerfile, _context in _listed():
        assert f'"{image}"' not in script and dockerfile not in script, image
    assert ":latest" not in script, "no mutable tag is part of the release contract"


# --- ci.yml: the marker cannot be written before the gate ------------------------------


def _jobs() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _condition(job: dict) -> str:
    return " ".join(str(job.get("if", "")).split())


def _downstream_of(jobs: dict, root: str) -> set[str]:
    downstream: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, job in jobs.items():
            if name in downstream:
                continue
            if any(need == root or need in downstream for need in _needs(job)):
                downstream.add(name)
                changed = True
    return downstream


def _steps_running(jobs: dict, needle: str) -> list[tuple[str, dict]]:
    return [
        (name, step)
        for name, job in jobs.items()
        for step in job.get("steps", [])
        if needle in str(step.get("run", ""))
    ]


def test_only_one_step_writes_the_service_marker_and_it_waits_for_a_green_gate():
    jobs = _jobs()
    writers = _steps_running(jobs, RELEASE_STAGE)

    assert len(writers) == 1, f"exactly one step may write the service marker: {writers}"
    job_name, _step = writers[0]
    job = jobs[job_name]
    condition = _condition(job)

    assert "merge-gate" in _needs(job), "the marker job must depend on the Required CI Gate"
    assert "needs.merge-gate.result == 'success'" in condition, "only a green gate releases"
    # merge-gate is always(); without always() here a skipped conditional suite above the
    # gate would skip the release, as it once skipped the worker release.
    assert condition.startswith("always()"), condition
    for clause in PUSH_TO_MAIN:
        assert clause in condition, f"the service release is push-to-main only: {condition}"
    assert "workflow_dispatch" not in condition and "pull_request" not in condition
    assert "||" not in condition, f"no alternative path may bypass the gate: {condition}"


def test_no_job_outside_the_gate_can_write_the_service_marker():
    jobs = _jobs()
    after_gate = _downstream_of(jobs, "merge-gate")
    marker_writers = {
        name
        for needle in (RELEASE_STAGE, "release_marker_publish", f"/{MARKER_IMAGE}:")
        for name, _step in _steps_running(jobs, needle)
    }

    assert marker_writers, "the release step has to be found to be checked"
    assert marker_writers <= after_gate, (
        f"{sorted(marker_writers - after_gate)} can write the service marker without the gate"
    )


def test_candidates_are_built_beside_the_suites_on_push_to_main_only():
    jobs = _jobs()
    builders = _steps_running(jobs, CANDIDATE_STAGE)

    assert len(builders) == 1, builders
    job_name, step = builders[0]
    job = jobs[job_name]
    condition = _condition(job)
    for clause in PUSH_TO_MAIN:
        assert clause in condition
    assert "merge-gate" not in _needs(job), "the candidates may build in parallel with the suites"
    assert job_name not in _needs(jobs["merge-gate"]), (
        "the Required CI Gate on a pull request must not wait for a push-only build"
    )
    assert step["env"]["GIT_SHA"] == "${{ github.sha }}", "the tag is the merge SHA"
    assert any(
        s.get("uses") == "./.github/actions/setup-buildx-with-retry" for s in job["steps"]
    ), "the candidates are built with buildx"

    release_job = next(name for name, _step in _steps_running(jobs, RELEASE_STAGE))
    assert job_name in _needs(jobs[release_job]), "the release verifies this run's candidates"


def test_a_pull_request_never_gets_packages_write():
    for name, job in _jobs().items():
        if job.get("permissions", {}).get("packages") == "write":
            condition = _condition(job)
            for clause in PUSH_TO_MAIN:
                assert clause in condition, f"{name} has packages: write outside push to main"


def test_the_release_record_is_uploaded_and_summarised():
    jobs = _jobs()
    job_name, publish = _steps_running(jobs, RELEASE_STAGE)[0]
    steps = jobs[job_name]["steps"]
    record = publish["env"]["DIGEST_FILE"]

    upload = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert upload["with"]["path"] == record
    assert upload["with"]["if-no-files-found"] == "error"
    summary = next(s for s in steps if "GITHUB_STEP_SUMMARY" in str(s.get("run", "")))
    assert Path(record).name in summary["run"]
    assert steps.index(publish) < steps.index(upload) < steps.index(summary)


# --- The script, against a fake registry -----------------------------------------------

# A fake docker with a directory standing in for the registry. Per image name it keeps
# `<image>` (the digest its tag resolves to), `<image>.source` (the source hash label of
# that image) and, for a marker, `<image>.label` (the release record it was built with).
# `buildx build --push` writes the first two from --tag and --build-arg SOURCE_HASH, and
# fails for FAKE_FAILING_BUILD, which is how a run that dies mid-publish is injected.
# `buildx imagetools inspect` fails for a tag nothing pushed, `pull` fails for an image
# the registry does not hold, as a real registry answers. Nothing reaches a daemon.
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
        if [ "$1" = imagetools ]; then
            published="${FAKE_REGISTRY}/$(image_of "$3")"
            if [ ! -f "${published}" ]; then
                echo "ERROR: $3: not found" >&2
                exit 1
            fi
            cat "${published}"
            exit 0
        fi
        shift
        tag="" source=""
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --tag) tag="$2"; shift 2 ;;
                --build-arg) [[ "$2" == SOURCE_HASH=* ]] && source="${2#SOURCE_HASH=}"; shift 2 ;;
                *) shift ;;
            esac
        done
        name="$(image_of "${tag}")"
        if [ "${name}" = "${FAKE_FAILING_BUILD:-}" ]; then
            echo "ERROR: failed to build ${tag}" >&2
            exit 1
        fi
        echo "sha256:${name}" > "${FAKE_REGISTRY}/${name}"
        printf '%s' "${source}" > "${FAKE_REGISTRY}/${name}.source"
        ;;
    build)
        sed -n 's/^LABEL [^=]*="\(.*\)"$/\1/p' "$3/Dockerfile" \
            > "${FAKE_REGISTRY}/$(image_of "$2").label"
        ;;
    push)
        name="$(image_of "$1")"
        echo "sha256:${name}" > "${FAKE_REGISTRY}/${name}"
        ;;
    pull)
        if [ ! -f "${FAKE_REGISTRY}/$(image_of "$1")" ]; then
            echo "ERROR: $1: manifest unknown" >&2
            exit 1
        fi
        ;;
    inspect)
        name="$(image_of "$1")"
        if [[ "$*" == *service_release* ]]; then
            cat "${FAKE_REGISTRY}/${name}.label"
        elif [ -f "${FAKE_REGISTRY}/${name}.source" ]; then
            cat "${FAKE_REGISTRY}/${name}.source"
            echo
        else
            echo "<no value>"
        fi
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


class ServiceRelease:
    """publish-service-images.sh, run for real against one fake registry directory."""

    def __init__(self, tmp_path: Path, source_hash: str) -> None:
        self.root = tmp_path
        self.source_hash = source_hash
        binaries = tmp_path / "bin"
        binaries.mkdir()
        docker = binaries / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.binaries = binaries
        self.registry = tmp_path / "registry"
        self.registry.mkdir()
        self.log = tmp_path / "docker.log"
        self.record = tmp_path / "service-images.json"

    def run(self, stage: str | None, **overrides) -> tuple[subprocess.CompletedProcess, list[str]]:
        self.log.write_text("")
        environment = {
            "PATH": f"{self.binaries}:/usr/bin:/bin",
            "HOME": str(self.root),
            "FAKE_DOCKER_LOG": str(self.log),
            "FAKE_REGISTRY": str(self.registry),
            "GHCR_TOKEN": "test-token",
            "GHCR_OWNER": "test-owner",
            "GIT_SHA": PUBLISHED_SHA,
            "DIGEST_FILE": str(self.record),
        }
        environment.update(overrides)
        environment = {key: value for key, value in environment.items() if value is not None}
        command = ["bash", str(PUBLISH)] + ([stage] if stage else [])
        result = subprocess.run(
            command, capture_output=True, text=True, env=environment, cwd=self.root
        )
        return result, self.log.read_text().splitlines()

    def resolves(self, image: str) -> bool:
        return (self.registry / image).exists()

    def set_label(self, image: str, value: str) -> None:
        (self.registry / f"{image}.source").write_text(value)

    def marker_record(self) -> dict:
        payload = (self.registry / f"{MARKER_IMAGE}.label").read_text().strip()
        return json.loads(base64.b64decode(payload))

    def seed_candidates(self) -> None:
        for image in _names():
            (self.registry / image).write_text(f"sha256:{image}\n")
            self.set_label(image, self.source_hash)

    def seed_release(self, **record_overrides) -> None:
        """A SHA already released: every image, and the marker that commits them."""
        self.seed_candidates()
        record = {
            "schema_version": SCHEMA_VERSION,
            "git_sha": PUBLISHED_SHA,
            "source_hash": self.source_hash,
            "images": {
                image: {
                    "reference": f"{REGISTRY}/{image}@sha256:{image}",
                    "repository": f"{REGISTRY}/{image}",
                    "digest": f"sha256:{image}",
                }
                for image in _names()
            },
        }
        record.update(record_overrides)
        (self.registry / MARKER_IMAGE).write_text(f"sha256:{MARKER_IMAGE}\n")
        (self.registry / f"{MARKER_IMAGE}.label").write_text(
            base64.b64encode(json.dumps(record).encode()).decode()
        )


@pytest.fixture
def release(tmp_path, tree_source_hash) -> ServiceRelease:
    return ServiceRelease(tmp_path, tree_source_hash)


def _pushes(calls: list[str]) -> list[str]:
    """Every registry write: a plain push, or a buildx build that pushes."""
    return [
        call
        for call in calls
        if call.startswith("push ") or (call.startswith("buildx build") and "--push" in call)
    ]


def test_a_fresh_sha_pushes_every_candidate_once_and_the_marker_only_after_them(
    release, tree_source_hash
):
    built, build_calls = release.run("candidates")

    assert built.returncode == 0, built.stderr
    builds = [call for call in build_calls if call.startswith("buildx build")]
    assert len(builds) == len(_names())
    for image, dockerfile, context in _listed():
        [call] = [call for call in builds if f"--tag {REGISTRY}/{image}:{PUBLISHED_SHA} " in call]
        assert f"--file {dockerfile} " in call and call.endswith(f"--push {context}")
        assert f"--build-arg SOURCE_HASH={tree_source_hash} " in call
        assert f"--cache-from type=registry,ref={REGISTRY}/service-build-cache:{image}" in call
        assert f"--cache-to type=registry,ref={REGISTRY}/service-build-cache:{image}," in call
    assert not release.resolves(MARKER_IMAGE), "a candidate build never commits a release"
    assert not release.record.exists()

    released, calls = release.run("release")

    assert released.returncode == 0, released.stderr
    pushes = _pushes(calls)
    assert pushes == [f"push {REGISTRY}/{MARKER_IMAGE}:{PUBLISHED_SHA}"], (
        f"the release stage writes the marker and nothing else: {pushes}"
    )
    written = json.loads(release.record.read_text())
    assert written["schema_version"] == SCHEMA_VERSION
    assert written["git_sha"] == PUBLISHED_SHA
    assert written["source_hash"] == tree_source_hash
    assert set(written["images"]) == set(_names())
    for image in _names():
        entry = written["images"][image]
        assert entry["reference"] == f"{REGISTRY}/{image}@sha256:{image}"
    assert release.marker_record() == written, "the marker carries exactly the published record"
    resolutions = [call for call in calls if call.startswith("buildx imagetools")]
    assert len(resolutions) == len(_names()) + 1, "every tag is resolved exactly once"


def test_a_rerun_of_a_released_sha_verifies_and_pushes_nothing(release, tree_source_hash):
    release.seed_release()

    built, build_calls = release.run("candidates")
    assert built.returncode == 0, built.stderr
    assert not _pushes(build_calls), (
        f"a released SHA's candidates are never pushed over: {build_calls}"
    )

    released, calls = release.run("release")
    assert released.returncode == 0, released.stderr
    assert not _pushes(calls), f"a released SHA is never rewritten: {calls}"
    assert not [call for call in calls if call.startswith("build ")]
    written = json.loads(release.record.read_text())
    assert written["source_hash"] == tree_source_hash
    assert written == release.marker_record()


def test_a_publish_that_dies_mid_way_releases_nothing_and_a_rerun_completes_it(release):
    built, _calls = release.run("candidates", FAKE_FAILING_BUILD="scheduler")

    assert built.returncode == EXIT_BUILD, built.stderr
    assert release.resolves("api"), "the first candidates landed; this is the residue"
    assert not release.resolves("scheduler")

    released, calls = release.run("release")

    assert released.returncode == EXIT_UNRESOLVED, released.stderr
    assert "scheduler" in released.stderr
    assert not release.resolves(MARKER_IMAGE), "residue must never be committed as a release"
    assert not _pushes(calls)
    assert not release.record.exists()

    assert release.run("candidates")[0].returncode == 0, "a rerun pushes over its own residue"
    completed, _calls = release.run("release")
    assert completed.returncode == 0, completed.stderr
    assert release.resolves(MARKER_IMAGE)


@pytest.mark.parametrize(
    ("label", "expected_exit"),
    [("", EXIT_CANDIDATE_NO_LABEL), ("dead0000dead0000", EXIT_CANDIDATE_LABEL)],
    ids=["empty", "wrong"],
)
def test_a_candidate_without_this_trees_source_hash_is_never_released(
    release, label, expected_exit
):
    release.seed_candidates()
    release.set_label("admin-frontend", label)

    result, calls = release.run("release")

    assert result.returncode == expected_exit, result.stderr
    assert "admin-frontend" in result.stderr
    assert not release.resolves(MARKER_IMAGE)
    assert not _pushes(calls)
    assert not release.record.exists()


def test_a_candidate_absent_the_label_entirely_is_never_released(release):
    release.seed_candidates()
    (release.registry / "langgraph.source").unlink()

    result, _calls = release.run("release")

    assert result.returncode == EXIT_CANDIDATE_NO_LABEL, result.stderr
    assert not release.resolves(MARKER_IMAGE)


@pytest.mark.parametrize("label", ["", "dead0000dead0000"], ids=["empty", "wrong"])
def test_a_released_sha_with_a_bad_label_is_refused_not_rewritten(release, label):
    release.seed_release()
    release.set_label("worker-broker", label)

    result, calls = release.run("release")

    assert result.returncode == EXIT_RELEASED_LABEL, result.stderr
    assert "worker-broker" in result.stderr
    assert not _pushes(calls)


def test_a_released_sha_whose_image_is_gone_is_refused_not_repaired(release):
    release.seed_release()
    (release.registry / "user-dashboard").unlink()

    result, calls = release.run("release")

    assert result.returncode == EXIT_BROKEN_RELEASE, result.stderr
    assert "user-dashboard" in result.stderr
    assert not _pushes(calls)


@pytest.mark.parametrize(
    "corruption",
    [
        {"git_sha": "f" * 40},
        {"schema_version": 2},
        {"images": {}},
    ],
    ids=["other-sha", "other-schema", "other-chain"],
)
def test_a_marker_that_is_not_this_releases_record_is_refused(release, corruption):
    release.seed_release(**corruption)

    result, calls = release.run("release")

    assert result.returncode == EXIT_BROKEN_RELEASE, result.stderr
    assert not _pushes(calls)


def test_usage_errors_have_their_own_exit_code(release):
    assert release.run(None)[0].returncode == EXIT_USAGE
    assert release.run("deploy")[0].returncode == EXIT_USAGE
    assert release.run("release", DIGEST_FILE=None)[0].returncode == EXIT_USAGE
    assert release.run("candidates", GIT_SHA=None)[0].returncode == EXIT_USAGE
