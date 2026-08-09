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
    build_routes,
    check,
    compose_routes,
    dockerfile_bakes_shared,
    dockerfiles_baking_shared,
    makefile_routes,
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

# A child of the worker base: it inherits the baked `shared` instead of copying it, and
# stamps which one it inherited.
CHILD = """\
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH
"""

MAKEFILE = """\
rebuild-worker-images:
\tdocker build --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \\
\t\t-t worker-base-common:latest \\
\t\t-t worker-base-common:$(WORKER_SOURCE_HASH) \\
\t\t-f services/worker-manager/images/worker-base-common/Dockerfile .
\tdocker build --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \\
\t\t--build-arg BASE_IMAGE=worker-base-common:$(WORKER_SOURCE_HASH) \\
\t\t-t worker-base-claude:latest \\
\t\t-f services/worker-manager/images/worker-base-claude/Dockerfile .

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
    _write(tmp_path, "services/worker-manager/images/worker-base-claude/Dockerfile", CHILD)
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


def test_a_tree_without_docker_answers_the_same_as_one_with_it(tree: Path, monkeypatch):
    """No docker binary: every image reads as not built, and the static rules still hold."""

    def no_docker(*_args, **_kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", no_docker)

    assert check(tree, report=lambda _: None) == []

    _write(tree, "services/newcomer/Dockerfile", LABELLED)

    problems = check(tree, report=lambda _: None)

    assert len(problems) == 1
    assert problems[0].startswith("services/newcomer/Dockerfile: bakes shared but no build route")


def test_a_daemon_that_cannot_be_reached_is_not_staleness(tree: Path, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon at unix://",
        ),
    )

    assert check(tree, report=lambda _: None) == []


def test_an_inspect_that_fails_for_another_reason_is_not_swallowed(tree: Path, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="permission denied while trying to connect"
        ),
    )

    with pytest.raises(RuntimeError, match="docker image inspect"):
        check(tree, report=lambda _: None)


# --- reading Dockerfiles: the forms docker accepts -----------------------------


def test_a_dockerfile_baking_shared_without_the_label_is_uncovered(tree: Path):
    _write(tree, "services/newcomer/Dockerfile", UNLABELLED)

    problems = check(tree, inspect=_inspector(_built(tree)), report=lambda _: None)

    assert "services/newcomer/Dockerfile: bakes shared but declares no ARG SOURCE_HASH" in problems


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
        "worker-base-claude:latest",
        "codegen-orchestrator/worker-manager:local",
        "codegen-orchestrator/api:test",
    }


def test_a_compose_service_without_an_image_name_fails_the_check(tree: Path):
    (tree / "docker/test/api.yml").write_text(
        TEST_COMPOSE.replace("    image: codegen-orchestrator/api:test\n", "")
    )

    problems, _ = compose_routes(tree)

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


def test_an_interpolated_image_name_is_an_unreadable_route(tree: Path):
    """`${VAR}` is resolved outside the tree, so it names nothing this check can inspect.

    The image compose really builds — say `verified/newcomer:real` — would never be looked
    at, and the check would pass while it held an old `shared`. The same service under a
    literal name is `test_the_same_dockerfile_on_a_compose_route_passes`, which uses this
    very compose file unmodified.
    """
    _write(tree, "services/newcomer/Dockerfile", LABELLED)
    _write(
        tree,
        "docker/test/newcomer.yml",
        ROUTED_COMPOSE.replace(
            "    image: codegen-orchestrator/newcomer:local\n", "    image: ${NEWCOMER_IMAGE}\n"
        ),
    )
    images = _built(tree)
    images["verified/newcomer:real"] = {SOURCE_HASH_LABEL: "0000000000000000"}

    problems = check(tree, inspect=_inspector(images), report=lambda _: None)

    assert len(problems) == 1
    assert problems[0].startswith(
        "docker/test/newcomer.yml: service newcomer builds services/newcomer/Dockerfile"
    )
    assert "not a literal" in problems[0]


def test_a_compose_file_that_cannot_be_parsed_fails_the_check(tree: Path):
    _write(tree, "docker/test/broken.yml", "services:\n  api:\n   - build: [\n")

    problems = check(tree, inspect=_inspector(_built(tree)), report=lambda _: None)

    assert len(problems) == 1
    assert problems[0].startswith("docker/test/broken.yml: has services: but cannot be parsed")


def test_a_yaml_that_is_not_compose_is_left_alone(tree: Path):
    _write(tree, "some/config.yml", "not: [a, compose, file\n")

    assert check(tree, inspect=_inspector(_built(tree)), report=lambda _: None) == []


# --- every Dockerfile reaches a name: the routes -------------------------------

ROUTED_COMPOSE = """\
services:
  newcomer:
    image: codegen-orchestrator/newcomer:local
    build:
      context: ../..
      dockerfile: services/newcomer/Dockerfile
      args:
        SOURCE_HASH: ${WORKER_SOURCE_HASH:-}
"""

ROUTED_RECIPE = """\

build-newcomer:
\tdocker build --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \\
\t\t-t codegen-orchestrator/newcomer:local \\
\t\t-f services/newcomer/Dockerfile .
"""


def test_a_dockerfile_no_route_builds_fails_the_check_by_name(tree: Path):
    """Correctly labelled and still uncomparable: nothing gives the image a name."""
    _write(tree, "services/newcomer/Dockerfile", LABELLED)

    problems = check(tree, inspect=_inspector(_built(tree)), report=lambda _: None)

    assert len(problems) == 1
    assert problems[0].startswith(
        "services/newcomer/Dockerfile: bakes shared but no build route gives it an image name"
    )


def test_the_same_dockerfile_on_a_compose_route_passes(tree: Path):
    _write(tree, "services/newcomer/Dockerfile", LABELLED)
    _write(tree, "docker/test/newcomer.yml", ROUTED_COMPOSE)

    assert check(tree, inspect=_inspector(_built(tree)), report=lambda _: None) == []
    assert "codegen-orchestrator/newcomer:local" in {
        image.reference for image in tracked_images(tree)
    }


def test_the_same_dockerfile_on_a_makefile_route_passes(tree: Path):
    _write(tree, "services/newcomer/Dockerfile", LABELLED)
    (tree / "Makefile").write_text(MAKEFILE + ROUTED_RECIPE)

    assert check(tree, inspect=_inspector(_built(tree)), report=lambda _: None) == []
    origins = {image.reference: image.origin for image in tracked_images(tree)}
    assert origins["codegen-orchestrator/newcomer:local"] == "make build-newcomer"


def test_a_recipe_that_builds_a_baker_under_no_tag_fails_the_check(tree: Path):
    (tree / "Makefile").write_text(
        MAKEFILE + ROUTED_RECIPE.replace("\t\t-t codegen-orchestrator/newcomer:local \\\n", "")
    )
    _write(tree, "services/newcomer/Dockerfile", LABELLED)

    problems, _ = makefile_routes(tree)

    assert problems == [
        "Makefile recipe build-newcomer builds services/newcomer/Dockerfile, which bakes "
        "shared, without an explicit -t name that can be found again afterwards"
    ]


def test_a_recipe_that_builds_a_baker_without_the_hash_fails_the_check(tree: Path):
    (tree / "Makefile").write_text(
        MAKEFILE + ROUTED_RECIPE.replace("--build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) ", "")
    )
    _write(tree, "services/newcomer/Dockerfile", LABELLED)

    problems, _ = makefile_routes(tree)

    assert problems == [
        "Makefile recipe build-newcomer builds services/newcomer/Dockerfile, which bakes "
        "shared, without passing --build-arg SOURCE_HASH"
    ]


def test_a_recipe_that_does_not_say_what_it_builds_fails_the_check(tree: Path):
    (tree / "Makefile").write_text(MAKEFILE + "\nbuild-mystery:\n\tdocker build -t mystery .\n")

    with pytest.raises(Unreadable, match="cannot tell which Dockerfile"):
        makefile_routes(tree)


def test_a_child_image_that_stamps_the_hash_is_compared_even_without_baking(tree: Path):
    """`FROM ${BASE_IMAGE}` inherits the baked copy, and says which one by the label."""
    references = {image.reference for image in tracked_images(tree)}

    assert "worker-base-claude:latest" in references


# --- the repository itself ---------------------------------------------------


def test_every_dockerfile_in_this_repository_is_covered():
    assert uncovered_dockerfiles(REPO_ROOT) == []


def test_every_dockerfile_in_this_repository_reaches_an_image_name():
    """Totality on the real tree: a Dockerfile no route builds fails this."""
    problems, _routes = build_routes(REPO_ROOT)

    assert problems == []


def test_every_compose_service_in_this_repository_can_be_checked():
    """Including docker/test/**: a built test image is an image like any other."""
    problems, _ = compose_routes(REPO_ROOT)

    assert problems == []


def test_the_tracked_set_covers_the_images_that_bake_shared_and_are_reused():
    references = {image.reference for image in tracked_images(REPO_ROOT)}

    assert references == {
        "worker-base-common:latest",
        "worker-base-claude:latest",
        "worker-base-factory:latest",
        "worker-base-codex:latest",
        "codegen-orchestrator/worker-manager:local",
        "codegen-orchestrator/worker-broker:local",
        "codegen-orchestrator/worker-manager:test",
        "codegen-orchestrator/worker-broker:test",
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
