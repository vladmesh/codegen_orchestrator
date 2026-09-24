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
    (image_tree / "tests").mkdir()
    (image_tree / "tests/compose.yml").write_text(
        "services:\n  cache:\n    image: redis:7.4.10-alpine\n  db:\n    image: postgres:latest\n"
    )

    with pytest.raises(SystemExit, match=r"tests/compose.yml:5 \(postgres:latest\)"):
        gate.assert_pinned_base_images()


@pytest.mark.parametrize(
    "image_line",
    [
        "    image: postgres:latest\n",
        "    image: postgres:latest # explanation\n",
        '    image: "postgres:latest"  # explanation\n',
    ],
)
def test_an_inline_comment_does_not_hide_a_floating_compose_image(gate, image_tree, image_line):
    """The value and its line come off one parse, so a comment cannot split them."""
    (image_tree / "tests").mkdir()
    (image_tree / "tests/compose.yml").write_text(f"services:\n  database:\n{image_line}")

    with pytest.raises(SystemExit, match=r"tests/compose.yml:3 \(postgres:latest\)"):
        gate.assert_pinned_base_images()


def test_an_inline_comment_does_not_fail_a_pinned_compose_image(gate, image_tree):
    (image_tree / "tests").mkdir()
    (image_tree / "tests/compose.yml").write_text(
        "services:\n  database:\n    image: postgres:16-alpine # pinned on purpose\n"
    )

    gate.assert_pinned_base_images()


def _write_compose(root: Path, body: str) -> None:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests/compose.yml").write_text(body)


def test_a_merged_floating_image_fails_the_gate(gate, image_tree):
    """Compose resolves `<<` into the service, so an unfollowed merge hides an image."""
    _write_compose(
        image_tree, "x-base: &base\n  image: redis:latest\nservices:\n  cache:\n    <<: *base\n"
    )

    with pytest.raises(SystemExit, match=r"tests/compose.yml:2 \(redis:latest\)"):
        gate.assert_pinned_base_images()


def test_a_merged_pinned_image_passes(gate, image_tree):
    _write_compose(
        image_tree,
        "x-base: &base\n  image: redis:7.4.10-alpine\nservices:\n  cache:\n    <<: *base\n",
    )

    gate.assert_pinned_base_images()


def test_a_chain_of_merges_is_followed(gate, image_tree):
    _write_compose(
        image_tree,
        "x-a: &a\n  image: redis:latest\nx-b: &b\n  <<: *a\nservices:\n  cache:\n    <<: *b\n",
    )

    with pytest.raises(SystemExit, match=r"tests/compose.yml:2 \(redis:latest\)"):
        gate.assert_pinned_base_images()


def test_an_image_on_the_service_wins_over_a_merged_one(gate, image_tree):
    _write_compose(
        image_tree,
        "x-base: &base\n  image: redis:latest\n"
        "services:\n  cache:\n    <<: *base\n    image: redis:7.4.10-alpine\n",
    )

    gate.assert_pinned_base_images()


def test_extends_fails_the_gate_by_name(gate, image_tree):
    """Not resolved is not passed: extends reaches an image this walk never reads."""
    _write_compose(
        image_tree,
        "services:\n  cache:\n    extends:\n      file: other.yml\n      service: base\n",
    )

    with pytest.raises(SystemExit, match="service cache uses extends"):
        gate.assert_pinned_base_images()


def test_a_service_that_only_builds_is_not_a_violation(gate, image_tree):
    """It has no image to pin; the Dockerfile it builds is walked on its own."""
    _write_dockerfile(image_tree, "FROM python:3.12.13-slim\n")
    _write_compose(image_tree, "services:\n  app:\n    build:\n      context: ../service\n")

    gate.assert_pinned_base_images()


def test_an_interpolated_image_stays_floating(gate, image_tree):
    _write_compose(image_tree, "services:\n  app:\n    image: ${APP_IMAGE}\n")

    with pytest.raises(SystemExit, match=r"tests/compose.yml:3 \(\$\{APP_IMAGE\}\)"):
        gate.assert_pinned_base_images()


def test_an_image_that_is_not_a_single_value_fails_the_gate(gate, image_tree):
    _write_compose(image_tree, "services:\n  app:\n    image:\n      - redis:7.4.10-alpine\n")

    with pytest.raises(SystemExit, match="service app has an image that is not a single value"):
        gate.assert_pinned_base_images()


def test_a_merge_of_something_that_is_not_a_mapping_fails_the_gate(gate, image_tree):
    _write_compose(image_tree, 'x-base: &base "text"\nservices:\n  cache:\n    <<: *base\n')

    with pytest.raises(SystemExit, match="service cache merges something that is not a mapping"):
        gate.assert_pinned_base_images()


def test_a_service_that_is_not_a_mapping_fails_the_gate(gate, image_tree):
    _write_compose(image_tree, "services:\n  cache: redis:latest\n")

    with pytest.raises(SystemExit, match="service cache is not a mapping"):
        gate.assert_pinned_base_images()


def test_an_undefined_anchor_fails_the_gate_instead_of_raising(gate, image_tree):
    _write_compose(image_tree, "services:\n  cache:\n    <<: *missing\n")

    with pytest.raises(SystemExit, match="does not parse as YAML"):
        gate.assert_pinned_base_images()


def test_yaml_that_is_not_a_compose_file_is_left_alone(gate, image_tree):
    """A workflow's jobs mapping and an ansible task list are not services."""
    _write_dockerfile(image_tree, "FROM python:3.12.13-slim\n")
    (image_tree / "workflow.yml").write_text("jobs:\n  build:\n    steps:\n      - run: make\n")
    (image_tree / "tasks.yml").write_text("- name: run a container\n  image: redis:latest\n")

    gate.assert_pinned_base_images()


@pytest.mark.parametrize("body", ["FROM redis:latest\n", "FROM redis:latest # explanation\n"])
def test_an_inline_comment_does_not_hide_a_floating_dockerfile_image(gate, image_tree, body):
    _write_dockerfile(image_tree, body)

    with pytest.raises(SystemExit, match=r"service/Dockerfile:1 \(redis:latest\)"):
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
    """The real tree, globbed independently of the gate's own walk.

    The tree currently holds no `*_test.py` file, so this says nothing until one
    appears; that the walk reads the pattern at all is asserted on a synthetic tree
    by test_walk_finds_both_default_pytest_patterns.
    """
    suffix_dirs = {
        str(path.relative_to(gate.ROOT).parent)
        for path in gate.ROOT.rglob("*_test.py")
        if not gate.TEST_TREE_SKIP_DIRS.intersection(path.relative_to(gate.ROOT).parts)
    }
    assert suffix_dirs <= gate.discover_test_dirs()


def test_service_image_imports_are_required_by_the_ci_gate(gate):
    """A service module must import from the production image CI builds."""
    workflow = gate.load_workflow()

    gate.assert_service_image_imports(workflow["jobs"])


CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"


@pytest.fixture
def action_tree(gate, tmp_path, monkeypatch):
    """The gate pointed at an empty tree holding one workflow."""
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    (tmp_path / ".github/workflows").mkdir(parents=True)
    return tmp_path


def _write_workflow_step(root: Path, step: str) -> Path:
    workflow = root / ".github/workflows/ci.yml"
    workflow.write_text(
        f"jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - {step}\n"
    )
    return workflow


def _write_local_action(root: Path, uses: str) -> None:
    (root / ".github/actions/retry").mkdir(parents=True)
    (root / ".github/actions/retry/action.yml").write_text(
        f"runs:\n  using: composite\n  steps:\n    - uses: {uses}\n"
    )


def test_an_action_pinned_to_a_commit_passes(gate, action_tree):
    workflow = _write_workflow_step(action_tree, f"uses: actions/checkout@{CHECKOUT_SHA} # v7")

    gate.assert_pinned_actions(workflow)


@pytest.mark.parametrize(
    "reference",
    [
        "actions/checkout@v7 # v7",
        f"actions/checkout@{CHECKOUT_SHA}",
        f"actions/checkout@{CHECKOUT_SHA[:12]} # v7",
        f"actions/checkout@{CHECKOUT_SHA.upper()} # v7",
        "docker://alpine:3.20",
    ],
)
def test_an_unpinned_action_is_refused_by_line(gate, action_tree, reference):
    """A tag, a SHA with no tag comment, a short SHA: each is refused where it is written."""
    workflow = _write_workflow_step(action_tree, f"uses: {reference}")

    with pytest.raises(SystemExit, match=r"\.github/workflows/ci\.yml:5 \("):
        gate.assert_pinned_actions(workflow)


def test_an_unpinned_action_inside_a_local_action_is_refused(gate, action_tree):
    workflow = _write_workflow_step(action_tree, "uses: ./.github/actions/retry")
    _write_local_action(action_tree, "docker/setup-buildx-action@v3")

    with pytest.raises(
        SystemExit,
        match=r"\.github/actions/retry/action\.yml:4 \(docker/setup-buildx-action@v3\)",
    ):
        gate.assert_pinned_actions(workflow)


def test_a_pinned_action_inside_a_local_action_passes(gate, action_tree):
    workflow = _write_workflow_step(action_tree, "uses: ./.github/actions/retry")
    _write_local_action(action_tree, f"docker/setup-buildx-action@{CHECKOUT_SHA} # v3")

    gate.assert_pinned_actions(workflow)


def test_a_local_action_missing_from_the_tree_fails(gate, action_tree):
    workflow = _write_workflow_step(action_tree, "uses: ./.github/actions/missing")

    with pytest.raises(SystemExit, match="which is not in the tree"):
        gate.assert_pinned_actions(workflow)


def test_the_repository_workflow_pins_every_action(gate):
    gate.assert_pinned_actions()


def test_an_install_without_retry_fails_the_gate(gate):
    jobs = gate.load_workflow()["jobs"]
    gate.step_by_name(jobs["ci-contract"], "Install uv")["run"] = "pip install uv"

    with pytest.raises(SystemExit, match="ci-contract must install uv through"):
        gate.assert_download_retries(jobs)


def test_a_test_job_without_its_image_pull_fails_the_gate(gate):
    jobs = gate.load_workflow()["jobs"]
    steps = jobs["test-service"]["steps"]
    steps.remove(gate.step_by_id(jobs["test-service"], "pull-images"))

    with pytest.raises(SystemExit, match="missing step id pull-images"):
        gate.assert_download_retries(jobs)


def test_a_matrix_leg_without_its_marker_output_fails_the_gate(gate):
    jobs = gate.load_workflow()["jobs"]
    del jobs["test-integration"]["outputs"]["infra-marker-po-tools"]

    with pytest.raises(SystemExit, match="test-integration outputs must be exactly one"):
        gate.assert_infra_marker_exposed(jobs)


def test_the_publish_job_exposes_its_own_marker(gate):
    """Downstream of the gate, the publish job names its marker on itself."""
    jobs = gate.load_workflow()["jobs"]
    publish = jobs["publish-worker-images"]
    publish["steps"].remove(gate.step_by_name(publish, "Expose CI infrastructure failure"))

    with pytest.raises(SystemExit, match="missing step Expose CI infrastructure failure"):
        gate.assert_infra_marker_exposed(jobs)


def test_a_gate_that_does_not_repeat_markers_fails(gate):
    jobs = gate.load_workflow()["jobs"]
    gate.step_by_name(jobs["merge-gate"], "Check required jobs").pop("env")

    with pytest.raises(SystemExit, match="toJSON"):
        gate.assert_gate(jobs)


def test_the_repository_workflow_bounds_every_job(gate):
    gate.assert_job_timeouts(gate.load_workflow()["jobs"])


@pytest.mark.parametrize(
    "job_name", ["test-service", "build-worker-images", "publish-worker-images", "web-checks"]
)
def test_a_job_without_timeout_minutes_fails_the_gate(gate, job_name):
    """A new job, docker or not, cannot fall back to GitHub's 360-minute default."""
    jobs = gate.load_workflow()["jobs"]
    del jobs[job_name]["timeout-minutes"]

    with pytest.raises(SystemExit, match=f"{job_name} has no timeout-minutes"):
        gate.assert_job_timeouts(jobs)


def test_a_new_job_without_timeout_minutes_fails_the_gate(gate):
    jobs = gate.load_workflow()["jobs"]
    jobs["build-images"] = {"runs-on": "ubuntu-latest", "steps": [{"run": "docker build ."}]}

    with pytest.raises(SystemExit, match="build-images has no timeout-minutes"):
        gate.assert_job_timeouts(jobs)


def test_a_job_timeout_above_the_ceiling_fails_the_gate(gate):
    jobs = gate.load_workflow()["jobs"]
    jobs["test-integration"]["timeout-minutes"] = 360

    with pytest.raises(SystemExit, match="above the 60-minute ceiling"):
        gate.assert_job_timeouts(jobs)


def test_a_step_bound_not_shorter_than_its_job_fails_the_gate(gate):
    """The job limit would stop the step before the step could name the timeout."""
    jobs = gate.load_workflow()["jobs"]
    minutes = jobs["test-integration"]["timeout-minutes"]
    step = gate.step_by_id(jobs["test-integration"], "integration-tests")
    step["run"] = step["run"].replace("--timeout 10m", f"--timeout {minutes}m")

    with pytest.raises(SystemExit, match=f"not shorter than the job's {minutes} minutes"):
        gate.assert_job_timeouts(jobs)


@pytest.mark.parametrize(
    "job_name,step_name",
    [
        (
            "publish-worker-images",
            "Verify the tested candidates and publish the worker release marker",
        ),
        ("build-worker-images", "Build and push the worker image candidates"),
        ("template-compatibility", "Run baseline compatibility smoke"),
    ],
)
def test_a_docker_step_without_its_bound_fails_the_gate(gate, job_name, step_name):
    jobs = gate.load_workflow()["jobs"]
    step = gate.step_by_name(jobs[job_name], step_name)
    step["run"] = gate.bounded_command(step)

    with pytest.raises(SystemExit, match=f"step {step_name} must run under"):
        gate.assert_job_timeouts(jobs)


def test_an_unbounded_test_step_fails_the_gate(gate):
    jobs = gate.load_workflow()["jobs"]
    gate.step_by_id(jobs["test-service"], "service-tests")["run"] = (
        "make test-service SERVICE=${{ matrix.service }}"
    )

    with pytest.raises(SystemExit, match="must run under scripts/ci-infra.sh bound"):
        gate.assert_service_tests(jobs)


def test_a_buildx_setup_without_the_pull_hang_simulation_fails_the_gate(gate):
    jobs = gate.load_workflow()["jobs"]
    del gate.step_by_name(jobs["test-service"], "Set up Docker Buildx with retry")["with"][
        "simulate_first_attempt_pull_hang"
    ]

    with pytest.raises(SystemExit, match="simulate_first_attempt_pull_hang"):
        gate.assert_service_tests(jobs)


# --- the worst case of a job fits inside its limit ----------------------------


def _budget_minutes(gate, jobs, job_name, **leg_values):
    job = jobs[job_name]
    (leg,) = [
        leg
        for leg in gate.matrix_legs(job)
        if all(leg.get(key) == value for key, value in leg_values.items())
    ]
    return gate.job_budget_seconds(job_name, job, leg) / 60


def test_a_bounded_retry_costs_every_attempt_at_its_bound_plus_kill_after_and_backoff(gate):
    # 3 x (120 s + 30 s kill-after) + 10 s + 20 s of backoff.
    assert gate.retry_worst_seconds("120s") == 480
    assert gate.retry_worst_seconds("90s") == 390


def test_the_budget_sums_every_step_up_to_the_bound_and_the_always_steps_after(gate):
    """test-integration/backend: checkout 2, Buildx 8, two images at 6.5, the tests'
    10 min plus kill-after, the always() assert 1 and cleanup 2, the 2-minute margin."""
    jobs = gate.load_workflow()["jobs"]

    assert _budget_minutes(gate, jobs, "test-integration", suite="backend") == 38.5
    # The template leg pulls nothing but sets up uv; its uv step is skipped elsewhere.
    assert _budget_minutes(gate, jobs, "test-integration", suite="template") == 28.5


def test_a_job_whose_worst_case_retries_and_test_bound_exceed_its_limit_fails(gate):
    """A job limit only above each step bound still stops the job before the bound can
    name a hang when earlier retries used their bounds: the round-1 limit of 15."""
    jobs = gate.load_workflow()["jobs"]
    jobs["test-integration"]["timeout-minutes"] = 15

    with pytest.raises(
        SystemExit, match="test-integration suite=backend can take 38.5 minutes .* 15"
    ):
        gate.assert_job_timeouts(jobs)


def test_the_leg_that_pulls_the_most_images_decides(gate):
    jobs = gate.load_workflow()["jobs"]
    assert _budget_minutes(gate, jobs, "test-service", service="scheduler") == 48
    jobs["test-service"]["timeout-minutes"] = 45

    with pytest.raises(SystemExit, match="test-service service=scheduler can take 48 minutes"):
        gate.assert_job_timeouts(jobs)


def test_a_longer_retry_attempt_bound_is_counted_three_times(gate):
    jobs = gate.load_workflow()["jobs"]
    pull = gate.step_by_name(jobs["fast-checks"], "Pull Redis image with retry")
    pull["run"] = pull["run"].replace("--attempt-timeout 90s", "--attempt-timeout 300s")

    with pytest.raises(SystemExit, match="fast-checks can take 52.5 minutes"):
        gate.assert_job_timeouts(jobs)


def test_an_unbounded_step_before_a_bounded_one_fails_the_gate(gate):
    """Its worst case is unknown, so no sum can show the bound fires inside the job."""
    jobs = gate.load_workflow()["jobs"]
    del gate.step_by_name(jobs["fast-checks"], "Run unit tests")["timeout-minutes"]

    with pytest.raises(SystemExit, match="fast-checks step Run unit tests has no bound"):
        gate.assert_job_timeouts(jobs)


def test_an_unbounded_always_step_after_the_bound_fails_the_gate(gate):
    jobs = gate.load_workflow()["jobs"]
    del gate.step_by_name(jobs["test-integration"], "Cleanup test containers")["timeout-minutes"]

    with pytest.raises(SystemExit, match="step Cleanup test containers has no bound"):
        gate.assert_job_timeouts(jobs)


def test_a_step_the_check_cannot_rule_out_counts_as_running(gate):
    """An `if` beyond a plain matrix comparison errs long: the step is counted."""
    jobs = gate.load_workflow()["jobs"]
    job = jobs["template-compatibility"]
    baseline = _budget_minutes(gate, jobs, "template-compatibility", entry="baseline")
    candidate = gate.step_by_name(job, "Run candidate compatibility smoke")
    candidate["if"] = "matrix.candidate == true || github.event_name == 'workflow_dispatch'"

    assert _budget_minutes(gate, jobs, "template-compatibility", entry="baseline") == (
        baseline + 15.5
    )


def test_the_redis_regression_outside_its_bound_fails_the_gate(gate):
    jobs = gate.load_workflow()["jobs"]
    step = gate.step_by_name(jobs["fast-checks"], "Run Redis capability cleanup regression")
    step["run"] = gate.bounded_command(step)

    with pytest.raises(SystemExit, match="step Run Redis capability cleanup regression must run"):
        gate.assert_job_timeouts(jobs)


def test_the_repository_workflow_releases_what_the_dind_suite_tested(gate):
    gate.assert_worker_release_tests_what_ships(gate.load_workflow()["jobs"])


def test_a_dind_suite_that_builds_its_own_worker_chain_fails_the_gate(gate):
    jobs = gate.load_workflow()["jobs"]
    suite = gate.step_by_id(jobs["test-backend-dind-integration"], "integration-tests")
    suite["env"]["WORKER_BASE_IMAGE_SOURCE"] = "build"

    with pytest.raises(SystemExit, match="must test the candidates"):
        gate.assert_worker_release_tests_what_ships(jobs)


def test_a_marker_job_that_commits_another_record_fails_the_gate(gate):
    jobs = gate.load_workflow()["jobs"]
    release = gate.step_by_name(
        jobs["publish-worker-images"],
        "Verify the tested candidates and publish the worker release marker",
    )
    release["env"]["WORKER_CANDIDATES"] = "${{ steps.rebuilt.outputs.candidates }}"

    with pytest.raises(SystemExit, match="must commit"):
        gate.assert_worker_release_tests_what_ships(jobs)


def test_a_build_after_the_gate_fails_the_gate(gate):
    jobs = gate.load_workflow()["jobs"]
    jobs["publish-worker-images"]["steps"].insert(1, {"run": "make rebuild-worker-images"})

    with pytest.raises(SystemExit, match="must build nothing"):
        gate.assert_worker_release_tests_what_ships(jobs)
