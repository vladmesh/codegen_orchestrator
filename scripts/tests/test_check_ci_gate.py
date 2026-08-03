"""The gate's tree walks: every test file pytest collects, every image a build pulls."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check-ci-gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_ci_gate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate():
    return _load_gate()


def _write_pyproject(root: Path, python_files: str | None) -> None:
    setting = "" if python_files is None else f"python_files = {python_files}\n"
    (root / "pyproject.toml").write_text(
        f"[tool.pytest.ini_options]\nasyncio_mode = 'auto'\n{setting}"
    )


def test_walk_finds_both_default_pytest_patterns(gate, tmp_path, monkeypatch):
    _write_pyproject(tmp_path, None)
    (tmp_path / "prefix/tests").mkdir(parents=True)
    (tmp_path / "prefix/tests/test_feature.py").write_text("def test_x():\n    assert True\n")
    (tmp_path / "suffix/tests").mkdir(parents=True)
    (tmp_path / "suffix/tests/feature_test.py").write_text("def test_x():\n    assert True\n")
    monkeypatch.setattr(gate, "ROOT", tmp_path)

    assert gate.discover_test_dirs() == {"prefix/tests", "suffix/tests"}


def test_patterns_fall_back_to_pytest_defaults_without_python_files(gate, tmp_path, monkeypatch):
    _write_pyproject(tmp_path, None)
    monkeypatch.setattr(gate, "ROOT", tmp_path)

    assert set(gate.test_file_patterns()) == {"test_*.py", "*_test.py"}


def test_patterns_follow_a_configured_python_files(gate, tmp_path, monkeypatch):
    _write_pyproject(tmp_path, '"check_*.py"')
    (tmp_path / "suite").mkdir()
    (tmp_path / "suite/check_feature.py").write_text("def test_x():\n    assert True\n")
    (tmp_path / "suite/test_feature.py").write_text("def test_x():\n    assert True\n")
    (tmp_path / "other").mkdir()
    (tmp_path / "other/test_feature.py").write_text("def test_x():\n    assert True\n")
    monkeypatch.setattr(gate, "ROOT", tmp_path)

    assert gate.test_file_patterns() == ("check_*.py",)
    assert gate.discover_test_dirs() == {"suite"}


def test_walk_skips_caches_and_virtualenvs(gate, tmp_path, monkeypatch):
    _write_pyproject(tmp_path, None)
    (tmp_path / ".venv/lib/tests").mkdir(parents=True)
    (tmp_path / ".venv/lib/tests/test_vendored.py").write_text("def test_x():\n    assert True\n")
    (tmp_path / "real").mkdir()
    (tmp_path / "real/test_mine.py").write_text("def test_x():\n    assert True\n")
    monkeypatch.setattr(gate, "ROOT", tmp_path)

    assert gate.discover_test_dirs() == {"real"}


def test_empty_walk_is_a_failure_not_a_pass(gate, tmp_path, monkeypatch):
    _write_pyproject(tmp_path, None)
    monkeypatch.setattr(gate, "ROOT", tmp_path)

    with pytest.raises(SystemExit, match="no test directories found"):
        gate.discover_test_dirs()


def test_a_file_claim_stops_at_that_file(gate):
    """pytest does not walk from a file argument to its siblings, so nor does a claim."""
    claims = {"suite/tests/test_claimed.py": "make test-integration-suite"}

    assert gate.claiming_target(claims, "suite/tests/test_claimed.py") == (
        "make test-integration-suite"
    )
    assert gate.claiming_target(claims, "suite/tests/test_sibling.py") is None


def test_a_directory_claim_reaches_every_descendant(gate):
    claims = {"suite/tests": "make test-unit suite suite"}

    assert gate.claiming_target(claims, "suite/tests/test_top.py") == "make test-unit suite suite"
    assert gate.claiming_target(claims, "suite/tests/deep/test_nested.py") == (
        "make test-unit suite suite"
    )


def test_a_file_argument_resolves_to_the_file_not_its_directory(gate):
    resolved = gate.resolve_test_path(
        gate.MAKEFILE, "tests/integration/template/test_stage5_mock_smoke.py", None
    )

    assert resolved == "tests/integration/template/test_stage5_mock_smoke.py"


def test_makefile_pytest_paths_read_a_host_run_without_its_flag_values(gate):
    paths = gate.makefile_pytest_paths("test-integration-template")

    assert paths == ["tests/integration/template/test_stage5_mock_smoke.py"]


@pytest.fixture
def image_tree(gate, tmp_path, monkeypatch):
    """The gate pointed at an empty tree, with the repository's exclusions dropped."""
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "UNPINNED_IMAGE_DIRS", {})
    monkeypatch.setattr(gate, "UNPINNED_IMAGE_REFS", {})
    return tmp_path


def _write_dockerfile(root: Path, body: str) -> None:
    (root / "service").mkdir(parents=True, exist_ok=True)
    (root / "service/Dockerfile").write_text(body)


def test_a_floating_dockerfile_tag_fails_the_gate(gate, image_tree):
    _write_dockerfile(image_tree, "FROM python:latest\n")

    with pytest.raises(SystemExit, match=r"service/Dockerfile:1 \(python:latest\)"):
        gate.assert_pinned_base_images()


def test_the_same_dockerfile_passes_once_the_tag_is_explicit(gate, image_tree):
    _write_dockerfile(image_tree, "FROM python:3.12.13-slim\n")

    gate.assert_pinned_base_images()


def test_a_missing_tag_counts_as_floating(gate, image_tree):
    _write_dockerfile(image_tree, "FROM redis\n")

    with pytest.raises(SystemExit, match=r"service/Dockerfile:1 \(redis\)"):
        gate.assert_pinned_base_images()


def test_a_floating_compose_image_fails_the_gate(gate, image_tree):
    (image_tree / "docker").mkdir()
    (image_tree / "docker/test.yml").write_text(
        "services:\n  cache:\n    image: redis:7.4.10-alpine\n  db:\n    image: postgres:latest\n"
    )

    with pytest.raises(SystemExit, match=r"docker/test.yml:5 \(postgres:latest\)"):
        gate.assert_pinned_base_images()


def test_an_excused_reference_passes_and_a_stale_excuse_fails(gate, image_tree, monkeypatch):
    _write_dockerfile(image_tree, "FROM python:latest\n")
    monkeypatch.setattr(
        gate, "UNPINNED_IMAGE_REFS", {"service/Dockerfile::python:latest": "a reason"}
    )
    gate.assert_pinned_base_images()

    _write_dockerfile(image_tree, "FROM python:3.12.13-slim\n")
    with pytest.raises(SystemExit, match="which is not a floating image in the tree"):
        gate.assert_pinned_base_images()


def test_an_excuse_without_a_reason_fails(gate, image_tree, monkeypatch):
    _write_dockerfile(image_tree, "FROM python:latest\n")
    monkeypatch.setattr(gate, "UNPINNED_IMAGE_REFS", {"service/Dockerfile::python:latest": "  "})

    with pytest.raises(SystemExit, match="has no reason"):
        gate.assert_pinned_base_images()


def test_stages_and_caller_supplied_build_args_are_not_image_references(gate, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "ARG BASE_IMAGE\n"
        "FROM python:3.12.13-slim AS builder\n"
        "FROM ${BASE_IMAGE}\n"
        "COPY --from=builder /install /usr/local\n"
        "COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /bin/\n"
    )

    assert gate.dockerfile_image_references(dockerfile) == [
        (2, "python:3.12.13-slim"),
        (5, "ghcr.io/astral-sh/uv:0.12.1"),
    ]


def test_a_build_arg_default_is_followed_to_the_image_it_names(gate, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("ARG BASE_IMAGE=worker-base-common:latest\nFROM ${BASE_IMAGE}\n")

    assert gate.dockerfile_image_references(dockerfile) == [(2, "worker-base-common:latest")]
    assert not gate.is_pinned_image("worker-base-common:latest")


def test_worker_base_children_take_their_base_from_the_builder(gate):
    """No default means a build that forgets BASE_IMAGE fails, rather than picking :latest."""
    images = gate.ROOT / "services/worker-manager/images"
    for child in ["worker-base-claude", "worker-base-codex", "worker-base-factory"]:
        body = (images / child / "Dockerfile").read_text()
        assert "ARG BASE_IMAGE\n" in body
        assert "ARG BASE_IMAGE=" not in body


def test_repo_tree_suffix_named_files_are_covered(gate):
    """The real tree, globbed independently of the gate's own walk."""
    suffix_dirs = {
        str(path.relative_to(gate.ROOT).parent)
        for path in gate.ROOT.rglob("*_test.py")
        if not gate.TEST_TREE_SKIP_DIRS.intersection(path.relative_to(gate.ROOT).parts)
    }
    assert suffix_dirs
    assert suffix_dirs <= gate.discover_test_dirs()
