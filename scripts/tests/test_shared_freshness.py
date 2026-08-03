"""The freshness check: what is built against what is in the tree.

Docker is not needed here — image inspection is a parameter of `check()`, so a test
hands it a dict of images the way `services/worker-manager/tests/unit/test_build_logic.py`
hands the manager a fake docker client. The synthetic tree below is a repository in
miniature: two source trees that go into the hash, a worker base image, a compose service
that runs its baked copy, one that is mounted over, and a test compose file whose images
survive the run that built them.
"""

from pathlib import Path
import subprocess

import pytest

from scripts.shared_freshness import (
    SOURCE_HASH_LABEL,
    Unreadable,
    check,
    compose_plan,
    dockerfile_bakes_shared,
    dockerfiles_baking_shared,
    source_hash,
    tracked_images,
    uncovered_dockerfiles,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

LABELLED = """\
FROM python:3.12-slim
COPY shared ./shared
ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH
"""

UNLABELLED = """\
FROM python:3.12-slim
COPY shared ./shared
"""

MAKEFILE = """\
rebuild-worker-images:
\tdocker build --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \\
\t\t-t worker-base-common:latest \\
\t\t-t worker-base-common:$(WORKER_SOURCE_HASH) \\
\t\t-f services/worker-manager/images/worker-base-common/Dockerfile .

other-target:
\t@echo not a build
"""

COMPOSE = """\
services:
  worker-manager:
    image: codegen-orchestrator/worker-manager:local
    build:
      context: .
      dockerfile: services/worker-manager/Dockerfile
      args:
        SOURCE_HASH: ${WORKER_SOURCE_HASH:-}
  api:
    image: codegen-orchestrator/api:local
    build:
      context: .
      dockerfile: services/api/Dockerfile
      args:
        SOURCE_HASH: ${WORKER_SOURCE_HASH:-}
    volumes:
      - ./shared:/app/shared:delegated
"""

TEST_COMPOSE = """\
services:
  api:
    image: codegen-orchestrator/api:test
    build:
      context: ../..
      dockerfile: services/api/Dockerfile
      args:
        SOURCE_HASH: ${WORKER_SOURCE_HASH:-}
"""


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A miniature repository: a worker base, a compose service, a test compose image."""
    _write(tmp_path, "shared/log_config.py", "SETTING = 1\n")
    _write(tmp_path, "packages/worker-wrapper/src/wrapper.py", "RUN = 1\n")
    _write(tmp_path, "services/worker-manager/images/worker-base-common/Dockerfile", LABELLED)
    _write(tmp_path, "services/worker-manager/Dockerfile", LABELLED)
    _write(tmp_path, "services/api/Dockerfile", LABELLED)
    _write(tmp_path, "Makefile", MAKEFILE)
    _write(tmp_path, "docker-compose.yml", COMPOSE)
    _write(tmp_path, "docker/test/api.yml", TEST_COMPOSE)
    return tmp_path


def _inspector(images: dict[str, dict[str, str]]):
    """docker image inspect, faked: an image absent from the dict is not built."""
    return lambda reference: images.get(reference)


def _built(tree: Path) -> dict[str, dict[str, str]]:
    stamp = {SOURCE_HASH_LABEL: source_hash(tree)}
    return {
        "worker-base-common:latest": dict(stamp),
        "codegen-orchestrator/worker-manager:local": dict(stamp),
        "codegen-orchestrator/api:test": dict(stamp),
    }


def test_images_stamped_with_the_current_tree_are_not_behind(tree: Path):
    assert check(tree, inspect=_inspector(_built(tree)), report=lambda _: None) == []


def test_an_edit_under_shared_leaves_the_built_images_behind(tree: Path):
    images = _built(tree)
    (tree / "shared" / "log_config.py").write_text("SETTING = 2\n")

    problems = check(tree, inspect=_inspector(images), report=lambda _: None)

    assert len(problems) == 3
    assert any("worker-base-common:latest" in problem for problem in problems)
    assert any("codegen-orchestrator/worker-manager:local" in problem for problem in problems)
    assert any("codegen-orchestrator/api:test" in problem for problem in problems)
    assert all(source_hash(tree) in problem for problem in problems)


def test_an_image_that_cannot_say_what_it_baked_fails_the_check(tree: Path):
    images = _built(tree)
    images["codegen-orchestrator/worker-manager:local"] = {"org.opencontainers.title": "wm"}

    problems = check(tree, inspect=_inspector(images), report=lambda _: None)

    assert problems == [
        f"codegen-orchestrator/worker-manager:local (docker-compose.yml service worker-manager) "
        f"is built without a {SOURCE_HASH_LABEL} label"
    ]


def test_a_built_test_image_without_a_label_fails_the_check_by_name(tree: Path):
    """A test image outlives the run that built it, so it is checked like any other."""
    images = _built(tree)
    images["codegen-orchestrator/api:test"] = {"com.docker.compose.project": "whatever"}

    problems = check(tree, inspect=_inspector(images), report=lambda _: None)

    assert problems == [
        f"codegen-orchestrator/api:test (docker/test/api.yml service api) is built without "
        f"a {SOURCE_HASH_LABEL} label"
    ]


def test_an_unparsable_label_fails_the_check(tree: Path):
    images = _built(tree)
    images["worker-base-common:latest"] = {SOURCE_HASH_LABEL: "unknown"}

    problems = check(tree, inspect=_inspector(images), report=lambda _: None)

    assert len(problems) == 1
    assert "worker-base-common:latest" in problems[0]
    assert "not a source hash" in problems[0]


def test_an_image_that_is_not_built_is_not_behind(tree: Path):
    reported: list[str] = []

    problems = check(tree, inspect=_inspector({}), report=reported.append)

    assert problems == []
    assert [line for line in reported if "not built" in line]


# --- reading Dockerfiles: the forms docker accepts -----------------------------


def test_a_dockerfile_baking_shared_without_the_label_is_uncovered(tree: Path):
    _write(tree, "services/newcomer/Dockerfile", UNLABELLED)

    problems = check(tree, inspect=_inspector(_built(tree)), report=lambda _: None)

    assert problems == [
        "services/newcomer/Dockerfile: bakes shared but declares no ARG SOURCE_HASH"
    ]


def test_a_dockerfile_that_stamps_nothing_with_its_arg_is_uncovered(tree: Path):
    _write(tree, "services/newcomer/Dockerfile", UNLABELLED + "ARG SOURCE_HASH=unknown\n")

    assert uncovered_dockerfiles(tree) == [
        ("services/newcomer/Dockerfile", f"bakes shared but sets no LABEL {SOURCE_HASH_LABEL}")
    ]


@pytest.mark.parametrize(
    "instruction",
    [
        "COPY shared ./shared",
        "COPY shared/ /app/shared/",
        "COPY --from=builder shared ./shared",
        'COPY ["shared", "/app/shared"]',
        'COPY --chown=1000:1000 ["shared", "other", "/app/"]',
        "COPY \\\n    shared \\\n    /app/shared",
        "COPY services/api/src ./src \\\n    # a comment inside the continuation\n",
    ],
)
def test_every_copy_form_docker_accepts_is_read(instruction: str):
    """Baking is read off the instruction, not guessed from one shape of it."""
    text = f"FROM python:3.12-slim\n{instruction}\n"

    bakes = dockerfile_bakes_shared(text, "Dockerfile")

    assert bakes == ("shared" in instruction)


@pytest.mark.parametrize(
    "instruction",
    [
        'COPY ["shared", /app/shared]',  # not JSON, docker would reject it
        "COPY ${SOURCES} /app/",  # a source that only the build knows
        "COPY *ared /app/shared",  # a glob where the top directory should be
        "COPY shared",  # no destination
    ],
)
def test_a_copy_that_cannot_be_read_fails_the_check_by_file(tree: Path, instruction: str):
    _write(tree, "services/newcomer/Dockerfile", f"FROM python:3.12-slim\n{instruction}\n")

    problems = check(tree, inspect=_inspector(_built(tree)), report=lambda _: None)

    assert len(problems) == 1
    assert problems[0].startswith("services/newcomer/Dockerfile: COPY")


def test_an_unreadable_copy_is_not_read_as_not_baking(tree: Path):
    _write(tree, "services/newcomer/Dockerfile", "FROM python:3.12-slim\nCOPY ${X} /app/\n")

    with pytest.raises(Unreadable, match="built out of a variable"):
        dockerfiles_baking_shared(tree)


# --- reading compose files: the static rule ------------------------------------


def test_a_mounted_compose_service_is_not_tracked(tree: Path):
    """`api` bakes shared too, but ./shared is mounted over it: nothing to go behind."""
    references = {image.reference for image in tracked_images(tree)}

    assert references == {
        "worker-base-common:latest",
        "codegen-orchestrator/worker-manager:local",
        "codegen-orchestrator/api:test",
    }


def test_a_compose_service_without_an_image_name_fails_the_check(tree: Path):
    (tree / "docker/test/api.yml").write_text(
        TEST_COMPOSE.replace("    image: codegen-orchestrator/api:test\n", "")
    )

    problems, _ = compose_plan(tree)

    assert len(problems) == 1
    assert problems[0].startswith("docker/test/api.yml: service api builds services/api/Dockerfile")
    assert "without declaring an image: name" in problems[0]


def test_a_compose_service_that_does_not_pass_the_hash_fails_the_check(tree: Path):
    (tree / "docker/test/api.yml").write_text(
        TEST_COMPOSE.replace("      args:\n        SOURCE_HASH: ${WORKER_SOURCE_HASH:-}\n", "")
    )

    problems = check(tree, inspect=_inspector(_built(tree)), report=lambda _: None)

    assert problems == [
        "docker/test/api.yml: service api builds services/api/Dockerfile, which bakes shared, "
        "without passing SOURCE_HASH in build.args"
    ]


def test_a_compose_file_that_cannot_be_parsed_fails_the_check(tree: Path):
    _write(tree, "docker/test/broken.yml", "services:\n  api:\n   - build: [\n")

    problems = check(tree, inspect=_inspector(_built(tree)), report=lambda _: None)

    assert len(problems) == 1
    assert problems[0].startswith("docker/test/broken.yml: has services: but cannot be parsed")


def test_a_yaml_that_is_not_compose_is_left_alone(tree: Path):
    _write(tree, "some/config.yml", "not: [a, compose, file\n")

    assert check(tree, inspect=_inspector(_built(tree)), report=lambda _: None) == []


# --- the repository itself ---------------------------------------------------


def test_every_dockerfile_in_this_repository_is_covered():
    assert uncovered_dockerfiles(REPO_ROOT) == []


def test_every_compose_service_in_this_repository_can_be_checked():
    """Including docker/test/**: a built test image is an image like any other."""
    problems, _ = compose_plan(REPO_ROOT)

    assert problems == []


def test_the_tracked_set_covers_the_images_that_bake_shared_and_are_reused():
    references = {image.reference for image in tracked_images(REPO_ROOT)}

    assert references == {
        "worker-base-common:latest",
        "worker-base-claude:latest",
        "worker-base-factory:latest",
        "worker-base-codex:latest",
        "codegen-orchestrator/worker-manager:local",
        "codegen-orchestrator/worker-manager:test",
        "codegen-orchestrator/api:test",
        "codegen-orchestrator/langgraph:test",
        "codegen-orchestrator/scheduler:test",
        "codegen-orchestrator/telegram_bot:test",
        "codegen-orchestrator/infra-service:test",
        "codegen-orchestrator/integration-test-runner:test",
    }


def test_worker_manager_bakes_shared_and_is_tracked():
    """The one compose service in the dev stack that runs the baked copy."""
    assert "services/worker-manager/Dockerfile" in dockerfiles_baking_shared(REPO_ROOT)

    origins = {
        image.reference: image.origin
        for image in tracked_images(REPO_ROOT)
        if image.dockerfile == "services/worker-manager/Dockerfile"
    }

    assert origins["codegen-orchestrator/worker-manager:local"] == (
        "docker-compose.yml service worker-manager"
    )


def test_the_makefile_reads_the_hash_from_here_and_counts_it_nowhere_else():
    """One counter, not two: the Makefile has to call this module, not repeat it."""
    makefile = (REPO_ROOT / "Makefile").read_text()

    assert "WORKER_SOURCE_HASH := $(shell python3 scripts/shared_freshness.py hash)" in makefile
    assert "sha256sum" not in makefile


def test_the_makefile_hash_equals_the_hash_this_module_computes():
    printed = subprocess.run(
        ["make", "-s", "print-source-hash"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert printed.stdout.strip() == source_hash(REPO_ROOT)


def test_the_fixtures_that_build_worker_images_use_the_same_producer():
    """The label those fixtures write has to be comparable with what this module says."""
    from tests.e2e import conftest as e2e_conftest
    from tests.integration.backend import conftest as backend_conftest

    assert backend_conftest.source_hash is source_hash
    assert e2e_conftest.source_hash is source_hash


def test_the_check_is_reachable_through_a_make_target():
    makefile = (REPO_ROOT / "Makefile").read_text()

    assert "check-shared-freshness:" in makefile
    assert "uv run python scripts/shared_freshness.py check" in makefile
