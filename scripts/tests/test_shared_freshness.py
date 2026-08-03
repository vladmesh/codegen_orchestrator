"""The freshness check: what is built against what is in the tree.

Docker is not needed here — image inspection is a parameter of `check()`, so a test
hands it a dict of images the way `services/worker-manager/tests/unit/test_build_logic.py`
hands the manager a fake docker client. The synthetic tree below is a repository in
miniature: two source trees that go into the hash, one Dockerfile that bakes `shared` and
is checked, and one that is mounted over.
"""

from pathlib import Path
import subprocess

import pytest

from scripts.shared_freshness import (
    SOURCE_HASH_LABEL,
    check,
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
  api:
    build:
      context: .
      dockerfile: services/api/Dockerfile
    volumes:
      - ./shared:/app/shared:delegated
"""


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A miniature repository with one tracked image and one mounted service."""
    _write(tmp_path, "shared/log_config.py", "SETTING = 1\n")
    _write(tmp_path, "packages/worker-wrapper/src/wrapper.py", "RUN = 1\n")
    _write(tmp_path, "services/worker-manager/images/worker-base-common/Dockerfile", LABELLED)
    _write(tmp_path, "services/worker-manager/Dockerfile", LABELLED)
    _write(tmp_path, "services/api/Dockerfile", LABELLED)
    _write(tmp_path, "Makefile", MAKEFILE)
    _write(tmp_path, "docker-compose.yml", COMPOSE)
    return tmp_path


def _inspector(images: dict[str, dict[str, str]]):
    """docker image inspect, faked: an image absent from the dict is not built."""
    return lambda reference: images.get(reference)


def _built(tree: Path) -> dict[str, dict[str, str]]:
    stamp = {SOURCE_HASH_LABEL: source_hash(tree)}
    return {
        "worker-base-common:latest": dict(stamp),
        "codegen-orchestrator/worker-manager:local": dict(stamp),
    }


def test_images_stamped_with_the_current_tree_are_not_behind(tree: Path):
    assert check(tree, inspect=_inspector(_built(tree)), report=lambda _: None) == []


def test_an_edit_under_shared_leaves_the_built_images_behind(tree: Path):
    images = _built(tree)
    (tree / "shared" / "log_config.py").write_text("SETTING = 2\n")

    problems = check(tree, inspect=_inspector(images), report=lambda _: None)

    assert len(problems) == 2
    assert any("worker-base-common:latest" in problem for problem in problems)
    assert any("codegen-orchestrator/worker-manager:local" in problem for problem in problems)
    assert all(source_hash(tree) in problem for problem in problems)


def test_an_image_that_cannot_say_what_it_baked_fails_the_check(tree: Path):
    images = _built(tree)
    images["codegen-orchestrator/worker-manager:local"] = {"org.opencontainers.title": "wm"}

    problems = check(tree, inspect=_inspector(images), report=lambda _: None)

    assert problems == [
        f"codegen-orchestrator/worker-manager:local (compose worker-manager) is built "
        f"without a {SOURCE_HASH_LABEL} label"
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


def test_a_mounted_compose_service_is_not_tracked(tree: Path):
    """`api` bakes shared too, but ./shared is mounted over it: nothing to go behind."""
    references = {image.reference for image in tracked_images(tree)}

    assert references == {"worker-base-common:latest", "codegen-orchestrator/worker-manager:local"}


def test_an_unnamed_compose_image_cannot_be_checked(tree: Path):
    (tree / "docker-compose.yml").write_text(COMPOSE.replace("    image: codegen", "    #image: c"))

    with pytest.raises(RuntimeError, match="declares no image: name"):
        tracked_images(tree)


# --- the repository itself ---------------------------------------------------


def test_every_dockerfile_in_this_repository_is_covered():
    assert uncovered_dockerfiles(REPO_ROOT) == []


def test_the_tracked_set_covers_the_images_that_bake_shared_and_are_reused():
    references = {image.reference for image in tracked_images(REPO_ROOT)}

    assert references == {
        "worker-base-common:latest",
        "worker-base-claude:latest",
        "worker-base-factory:latest",
        "worker-base-codex:latest",
        "codegen-orchestrator/worker-manager:local",
    }


def test_worker_manager_bakes_shared_and_is_tracked():
    """The one compose service that runs the baked copy, and the point of this check."""
    assert "services/worker-manager/Dockerfile" in dockerfiles_baking_shared(REPO_ROOT)

    tracked = {image.dockerfile: image for image in tracked_images(REPO_ROOT)}

    assert tracked["services/worker-manager/Dockerfile"].origin == "compose worker-manager"


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


def test_the_check_is_reachable_through_a_make_target():
    makefile = (REPO_ROOT / "Makefile").read_text()

    assert "check-shared-freshness:" in makefile
    assert "uv run python scripts/shared_freshness.py check" in makefile
