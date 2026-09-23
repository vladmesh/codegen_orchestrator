#!/usr/bin/env python3
"""Validate the GitHub Actions CI gate contract."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import sys
import tomllib
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
# The Makefile runs this gate by path, so `scripts/` is on sys.path and the repository
# root is not; the pin module can only be found from the tree.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.template_pin import TEMPLATE_PIN  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
BUILDX_RETRY_ACTION = ROOT / ".github" / "actions" / "setup-buildx-with-retry" / "action.yml"
BUILDX_BOOTSTRAP = BUILDX_RETRY_ACTION.parent / "bootstrap.sh"
UV_RETRY_ACTION = ROOT / ".github" / "actions" / "setup-uv-with-retry" / "action.yml"
CI_INFRA_HELPER = ROOT / "scripts" / "ci-infra.sh"
TEST_UNIT_LOCAL = ROOT / "scripts" / "test-unit-local.sh"
MAKEFILE = ROOT / "Makefile"
LINT_PATH_EXPR = "$(if $(LINT_PATH),$(LINT_PATH),.)"

SERVICE_COMPOSE_DIR = ROOT / "tests" / "compose" / "service"
INTEGRATION_COMPOSE_DIR = ROOT / "tests" / "compose" / "integration"

# Where /app points for each tests/compose/service compose file. The pytest paths in
# those commands are container-relative, so they only resolve with this.
SERVICE_COMPOSE_ROOTS = {
    "api": "services/api",
    "infra": "services/infra-service",
    "langgraph": "services/langgraph",
    "scheduler": "services/scheduler",
    "telegram_bot": "services/telegram_bot",
    "worker-manager": "services/worker-manager",
}
# Integration compose files that deliberately stay out of the PR matrix.
OUT_OF_PR_INTEGRATION_SUITES = {
    "backend-dind": (
        "Docker-in-Docker suite, run by ci.yml on pushes to main before worker-image "
        "publication; too expensive to run on every pull request"
    ),
}

# --- Test suite coverage ----------------------------------------------------
#
# A test file is any file in the tree whose name matches python_files of the root
# [tool.pytest.ini_options]; with no such setting the patterns are pytest's own
# defaults, test_*.py and *_test.py. The set is walked, never listed by hand, so a
# file added tomorrow shows up here on its own.
#
# Claims are read off the targets themselves: the ALL_SUITES table in
# scripts/test-unit-local.sh, the pytest commands in the compose files behind the
# test-service and test-integration matrices, and the pytest commands of an
# explicit Makefile target behind an integration suite.
#
# A claim covers exactly what the target it was read from executes: a directory
# argument covers that directory recursively, because pytest recurses into
# subdirectories, and a file argument covers that one file, because pytest does
# not walk from a file to its siblings. Nothing else widens a claim. A test file
# no claim covers has to be named in UNCLAIMED_TEST_FILES, or sit directly in a
# directory named in UNCLAIMED_TEST_DIRS.
PYTEST_DEFAULT_PYTHON_FILES = ("test_*.py", "*_test.py")
# Separated pytest flags whose value would otherwise be read as a path argument.
PYTEST_FLAGS_WITH_VALUE = {"-k", "-m", "-n", "-p", "--deselect", "--ignore", "--rootdir"}
TEST_TREE_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}

# Test directories no CI target runs, holding for the files directly in them and
# not for their subdirectories. Every line needs a reason; the point of the list
# is that skipping a suite is a decision on the record, not a default.
UNCLAIMED_TEST_DIRS = {
    "services/langgraph/tests/e2e": (
        "needs a real LLM API key (PO_LLM_API_KEY or ARCHITECT_LLM_API_KEY) and skips "
        "without one, so running "
        "it on a PR would only ever report a skip"
    ),
    "services/infra-service/tests/integration": (
        "red: test_provisioning_flow mocks neither the API client nor httpx, so "
        "process_provisioner_job opens a real connection; issue:39be2178e3658691977d"
    ),
    "tests/integration/worker_wrapper": (
        "red: test_worker_wrapper_lifecycle expects a /workspace git checkout that "
        "exists only inside a worker container; issue:576dccd5bbc42c48a794"
    ),
}

# Single test files no CI target runs, for directories where the rest of the files
# do run. Same rule as UNCLAIMED_TEST_DIRS: a reason per line.
UNCLAIMED_TEST_FILES = {
    "tests/integration/template/test_secrets_injection.py": (
        "the template suite runs an explicit file list, and this file has never "
        "been on it; issue:081da416652a2b0ad576"
    ),
    "tests/integration/template/test_workflow_validation.py": (
        "the template suite runs an explicit file list, and this file has never "
        "been on it; issue:081da416652a2b0ad576"
    ),
}

# --- Base image pins --------------------------------------------------------
#
# Every image a Dockerfile or a compose file of this repository builds on has to name
# what it wants: an explicit tag or a digest. A missing tag and :latest both mean "the
# registry decides", so the same tree builds differently on two days. The tree is
# walked the same way as the test files above, never listed by hand, so a Dockerfile
# added tomorrow is checked on its own.
#
# What is not a reference to pin: a stage of the same Dockerfile (FROM builder,
# COPY --from=builder), and a ${BUILD_ARG} the Dockerfile declares without a default.
# The second one is the fail-closed shape: the builder has to name the image, and a
# build that forgets to fails on a blank base name instead of picking up a stray tag.
FLOATING_IMAGE_TAG = "latest"
IMAGE_FILE_SKIP_DIRS = TEST_TREE_SKIP_DIRS
COMPOSE_MERGE_KEY = "<<"

# Trees whose image references are not this repository's to pin. Reason per line, same
# rule as the exclusions above: an unpinned image is a decision on the record.
UNPINNED_IMAGE_DIRS = {
    TEMPLATE_PIN.fixture_relpath: (
        "a vendored render of the pinned product kit, read by the template "
        "compatibility tests; its compose files belong to that repository, and "
        "editing them here would make the fixture stop matching the revision it "
        "fixes. The pins are the kit's to add"
    ),
}

# Single image references left floating on purpose, keyed by "<path>::<image>".
# Same rule again: a reason per line, and a stale entry fails the gate.
UNPINNED_IMAGE_REFS: dict[str, str] = {}

EXPECTED_GATE_NEEDS = {
    "detect-changes",
    "fast-checks",
    "service-image-imports",
    "ci-contract",
    "test-service",
    "test-integration",
    "template-compatibility",
    "web-checks",
    "test-backend-dind-integration",
}
EXPECTED_FILTERS = {
    "api",
    "langgraph",
    "scheduler",
    "telegram",
    "worker-manager",
    "shared",
    "packages",
    "infra-service",
    "docker-test",
    "ci",
    "deps",
    "integration-tests",
    "web",
}
HYPHENATED_OUTPUTS = {"worker-manager", "infra-service", "docker-test", "integration-tests"}
TEMPLATE_COMPAT_TIMEOUT_MINUTES = 30
BUILDX_RETRY_ATTEMPTS = 3
SIMULATED_REGISTRY_FAILURE_INPUT = "simulate_first_attempt_registry_failure"
SIMULATED_PULL_HANG_INPUT = "simulate_first_attempt_pull_hang"
SIMULATION_INPUTS = (SIMULATED_REGISTRY_FAILURE_INPUT, SIMULATED_PULL_HANG_INPUT)
BUILDX_SETUP_STEP = "Set up Docker Buildx in bounded attempts"
BUILDX_SETUP_COMMAND = (
    'bash "${GITHUB_WORKSPACE}/scripts/ci-infra.sh" retry --step setup-buildx '
    "--cause buildx-registry --attempt-timeout 120s -- "
    'bash "${GITHUB_ACTION_PATH}/bootstrap.sh"'
)
OFFLINE_LIVE_IGNORES = {
    "tests/live/test_api_crud.py",
    "tests/live/test_capability_cleanup_redis.py",
    "tests/live/test_ci_prompt.py",
    "tests/live/test_deploy_infra.py",
    "tests/live/test_full_pipeline.py",
    "tests/live/test_product_brief_pipeline.py",
    "tests/live/test_product_brief_package_pipeline.py",
    "tests/live/test_sprint_dod.py",
    "tests/live/test_health.py",
    "tests/live/test_pipeline_engineering.py",
    "tests/live/test_pipeline_scaffold.py",
    "tests/live/test_streams.py",
    "tests/live/test_supervisor.py",
}
UNIT_TEST_API_BASE_URL = "http://127.0.0.1:9"

# --- Third-party action pins ------------------------------------------------
#
# Every action ci.yml runs that is not in this repository names one commit: a full
# 40-character SHA, with the tag it was resolved from as a trailing "# vX" comment. A
# tag moves, and resolving it is one more control-plane call that can fail before the
# job starts. Local actions (./...) are followed, so an action they call is held to
# the same rule.
PINNED_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[^@\s]+)?@[0-9a-f]{40}$")
ACTION_VERSION_COMMENT = re.compile(r"#\s*v\d+(\.\d+)*\s*$")
LOCAL_ACTION_PREFIX = "./"

# --- CI infrastructure failure marker ---------------------------------------
#
# scripts/ci-infra.sh retries the downloads a job starts with and, once they are
# exhausted, writes one marker line: CI-INFRA-FAILURE: job=<job> step=<step>
# cause=<cause>. Each job below exposes its markers as job outputs, one per matrix
# leg, and merge-gate repeats them. docs/TESTING.md documents the format.
INFRA_MARKER_PREFIX = "CI-INFRA-FAILURE:"
INFRA_MARKER_FIELD = "[A-Za-z0-9._/-]+"
INFRA_MARKER_PATTERN = (
    f"{INFRA_MARKER_PREFIX} job={INFRA_MARKER_FIELD} step={INFRA_MARKER_FIELD} "
    f"cause={INFRA_MARKER_FIELD}"
)
INFRA_MARKER_ANNOTATION = "::error title=CI infrastructure failure::"
INFRA_EXPOSE_STEP = "Expose CI infrastructure failure"
INFRA_EXPOSE_CONDITION = "always() && hashFiles('scripts/ci-infra.sh') != ''"
INFRA_OUTPUT = "infra-marker"
# job -> the matrix key its legs are named by, or None for a single job.
INFRA_MARKER_JOBS: dict[str, str | None] = {
    "fast-checks": None,
    "ci-contract": None,
    "service-image-imports": None,
    "test-service": "service",
    "test-integration": "suite",
    "template-compatibility": "entry",
    "test-backend-dind-integration": None,
    # Downstream of merge-gate: the gate cannot repeat this marker, so it stays on the
    # job's own annotations, summary and output (docs/TESTING.md).
    "publish-worker-images": None,
    # Push-to-main only and outside the gate's needs, like the release job after it: both
    # keep their marker on their own annotations, summary and output.
    "build-service-images": None,
    "publish-service-release": None,
}
INSTALL_UV_COMMAND = (
    "bash scripts/ci-infra.sh retry --step install-uv --cause uv-download --attempt-timeout 60s "
    "-- pip install uv"
)
UV_SETUP_STEPS = {
    "test-integration": "Set up uv for template smoke",
    "template-compatibility": "Set up uv with retry",
}
PULL_IMAGES_STEP = "Pull test images with retry"
PULL_IMAGES_COMMANDS = {
    "test-service": (
        "bash scripts/ci-infra.sh pull-images --step pull-images "
        "tests/compose/service/${{ matrix.service }}.yml"
    ),
    "test-integration": (
        "bash scripts/ci-infra.sh pull-images --step pull-images "
        "tests/compose/integration/${{ matrix.suite }}.yml"
    ),
    "test-backend-dind-integration": (
        "bash scripts/ci-infra.sh pull-images --step pull-images "
        "tests/compose/integration/backend-dind.yml"
    ),
}
BACKEND_DIND_COMMAND = (
    "bash scripts/ci-infra.sh watch --step integration-tests -- make test-integration-backend-dind"
)
REDIS_PULL_STEP = "Pull Redis image with retry"
REDIS_PULL_COMMAND = (
    "bash scripts/ci-infra.sh retry --step redis-pull --cause image-pull --attempt-timeout 90s "
    "-- docker pull redis:7.4.10-alpine"
)

# --- Time bounds ------------------------------------------------------------
#
# Every job names its own timeout-minutes; without one GitHub waits 360 minutes, and a
# docker pull that neither fails nor finishes holds the job that long. No job gets more
# than JOB_TIMEOUT_CEILING_MINUTES: the longest measured job takes under 9 minutes, and
# raising the ceiling is a decision on the record, not a default.
#
# A step that builds, pulls or runs docker images runs under `ci-infra.sh bound`, and
# every Buildx bootstrap and image pull under a bounded `ci-infra.sh retry`. For the
# marker such a step writes when its bound fires to reach the always() expose step, the
# job limit must not fire first. So, per job and matrix leg, the worst case of every step
# that can run up to its last bounded step, plus the always() steps after it, plus
# JOB_BUDGET_MARGIN_SECONDS, must fit in timeout-minutes; a step counted there needs a
# bound of its own (a step timeout-minutes or a ci-infra.sh bound) or the budget is
# unknown. The step bounds are the measured durations with margin (docs/TESTING.md,
# "Time bounds").
JOB_TIMEOUT_CEILING_MINUTES = 60
# Set up job, the post steps, the expose step itself, and runner jitter: measured under
# half a minute together.
JOB_BUDGET_MARGIN_SECONDS = 120
CI_INFRA_BOUND = re.compile(
    r"^bash scripts/ci-infra\.sh bound --step (?P<step>[A-Za-z0-9._/-]+) "
    r"--timeout (?P<timeout>\S+) -- (?P<command>.+)$"
)
CI_INFRA_BOUNDED_RETRY = re.compile(
    r"^bash scripts/ci-infra\.sh retry "
    r"--step [A-Za-z0-9._/-]+ --cause [A-Za-z0-9._/-]+ --attempt-timeout (?P<timeout>\S+) -- "
)
CI_INFRA_PULL_IMAGES = re.compile(
    r"^bash scripts/ci-infra\.sh pull-images --step [A-Za-z0-9._/-]+ (?P<compose>\S+)$"
)
BUILDX_RETRY_USES = "./.github/actions/setup-buildx-with-retry"
MATRIX_EXPRESSION = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
MATRIX_CONDITION = re.compile(r"^\(?\s*matrix\.([A-Za-z0-9_-]+)\s*==\s*'?([^'\s()]+)'?\s*\)?$")
DURATION = re.compile(r"^(?P<value>[0-9]+)(?P<unit>[smh]?)$")
DURATION_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}
# job -> the docker steps it runs under a bound, by step name.
BOUNDED_DOCKER_STEPS = {
    "fast-checks": ["Run Redis capability cleanup regression"],
    "service-image-imports": ["Import every service entrypoint from its production image"],
    "test-service": ["Run service tests"],
    "test-integration": ["Run integration tests"],
    "template-compatibility": [
        "Run baseline compatibility smoke",
        "Run candidate compatibility smoke",
    ],
    "test-backend-dind-integration": ["Run integration tests"],
    "publish-worker-images": ["Build and publish the worker chain"],
    "build-service-images": ["Build and push the service image candidates"],
    "publish-service-release": ["Verify the candidates and publish the service release marker"],
}
REDIS_CLEANUP_COMMAND = "bash scripts/ci-redis-cleanup-regression.sh"


def fail(message: str) -> None:
    raise SystemExit(f"CI contract failed: {message}")


def load_workflow() -> dict[str, Any]:
    with WORKFLOW.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        fail("workflow root is not a mapping")
    return data


def require_job(jobs: dict[str, Any], name: str) -> dict[str, Any]:
    job = jobs.get(name)
    if not isinstance(job, dict):
        fail(f"missing job {name}")
    return job


def step_by_name(job: dict[str, Any], name: str) -> dict[str, Any]:
    for step in job.get("steps", []):
        if isinstance(step, dict) and step.get("name") == name:
            return step
    fail(f"missing step {name}")


def make_target_commands(target: str) -> list[str]:
    lines = MAKEFILE.read_text().splitlines()
    commands: list[str] = []
    in_target = False

    for line in lines:
        if not in_target:
            in_target = line == f"{target}:"
            continue
        if line and not line.startswith(("\t", " ")):
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        commands.append(stripped.removeprefix("@"))

    if not commands:
        fail(f"Makefile target {target} has no commands")
    return commands


def normalize_lint_command(command: str) -> str:
    return command.replace(LINT_PATH_EXPR, ".")


def step_by_id(job: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in job.get("steps", []):
        if isinstance(step, dict) and step.get("id") == step_id:
            return step
    fail(f"missing step id {step_id}")


def matrix_values(job: dict[str, Any], key: str) -> set[str]:
    include = job.get("strategy", {}).get("matrix", {}).get("include", [])
    if not isinstance(include, list):
        fail(f"job matrix for {key} is not a list")
    values = {item.get(key) for item in include if isinstance(item, dict)}
    if not all(isinstance(value, str) for value in values):
        fail(f"job matrix has non-string {key} values")
    return values


def output_reference(output: str) -> str:
    if output in HYPHENATED_OUTPUTS:
        return f"outputs['{output}']"
    return f"outputs.{output}"


def assert_detect_changes(jobs: dict[str, Any]) -> None:
    job = require_job(jobs, "detect-changes")
    outputs = set(job.get("outputs", {}).keys())
    missing = EXPECTED_FILTERS - outputs
    if missing:
        fail(f"detect-changes is missing outputs: {sorted(missing)}")
    workflow_text = WORKFLOW.read_text()
    for output in HYPHENATED_OUTPUTS:
        if f"outputs.{output}" in workflow_text:
            fail(f"hyphenated output {output} must use bracket syntax")

    filter_step = step_by_id(job, "filter")
    filters = filter_step.get("with", {}).get("filters", "")
    for filter_name in EXPECTED_FILTERS:
        if f"{filter_name}:" not in filters:
            fail(f"paths-filter is missing {filter_name}")
    for pattern in [
        ".github/workflows/**",
        "Makefile",
        "scripts/test-unit-local.sh",
        "scripts/check-ci-gate.py",
        "pyproject.toml",
        "uv.lock",
        "shared/**",
        "packages/**",
        "tests/compose/**",
        "tests/integration/**",
    ]:
        if pattern not in filters:
            fail(f"paths-filter is missing pattern {pattern}")


def assert_fast_checks(jobs: dict[str, Any]) -> None:
    job = require_job(jobs, "fast-checks")
    expected_lint_commands: list[str] = []
    for step_name, command in [
        ("Check formatting with Ruff", "uv run ruff format --check ."),
        ("Lint with Ruff", "uv run ruff check ."),
        ("Run unit tests", "make test-unit"),
    ]:
        step = step_by_name(job, step_name)
        if step.get("if"):
            fail(f"{step_name} must not be conditional")
        if step.get("run") != command:
            fail(f"{step_name} must run {command}")
        if step_name in {"Check formatting with Ruff", "Lint with Ruff"}:
            expected_lint_commands.append(command)
    lint_commands = [normalize_lint_command(command) for command in make_target_commands("lint")]
    positions = []
    for command in expected_lint_commands:
        try:
            positions.append(lint_commands.index(command))
        except ValueError:
            fail(f"make lint must cover CI Ruff command: {command}")
    if positions != sorted(positions):
        fail("make lint must run Ruff format check before Ruff lint check")
    step = step_by_name(job, "Run offline live regressions")
    if step.get("if"):
        fail("offline live regressions must not be conditional")
    if step.get("run") != "make test-live":
        fail("offline live regressions must call make test-live")
    for stale_step in [
        "Run live cleanup auth/FK regression",
        "Run live cleanup ssh_user regression",
        "Run live harness contract regression",
    ]:
        for candidate in job.get("steps", []):
            if isinstance(candidate, dict) and candidate.get("name") == stale_step:
                fail(f"fast-checks must not enumerate {stale_step}")


def assert_offline_live_unit_runner() -> None:
    script = TEST_UNIT_LOCAL.read_text()
    if f'API_BASE_URL="{UNIT_TEST_API_BASE_URL}"' not in script:
        fail("test-unit-local must use the unreachable unit-test API endpoint, not a host service")
    if "live-offline|tests/live|" not in script:
        fail("test-unit-local ALL_SUITES must include offline tests/live")
    for ignored in OFFLINE_LIVE_IGNORES:
        if f"--ignore={ignored}" not in script:
            fail(f"test-unit-local offline live suite is missing ignore {ignored}")


def assert_offline_live_make_target() -> None:
    makefile = MAKEFILE.read_text()
    command = "uv run pytest tests/live/ -v --tb=short $(LIVE_OFFLINE_IGNORE_FLAGS)"
    if command not in makefile:
        fail("make test-live must run tests/live/ through LIVE_OFFLINE_IGNORE_FLAGS")
    for ignored in OFFLINE_LIVE_IGNORES:
        if f"--ignore={ignored}" not in makefile:
            fail(f"make test-live is missing ignore {ignored}")


def compose_suites(directory: Path) -> set[str]:
    return {path.stem for path in directory.glob("*.yml")}


def pytest_paths(compose_file: Path) -> list[str]:
    """Every non-flag argument of the pytest commands in a compose file."""
    compose = yaml.safe_load(compose_file.read_text())
    if not isinstance(compose, dict):
        fail(f"{compose_file} is not a mapping")
    paths: list[str] = []
    for service in compose.get("services", {}).values():
        command = service.get("command") if isinstance(service, dict) else None
        if not isinstance(command, list) or not command or command[0] != "pytest":
            continue
        paths.extend(arg for arg in command[1:] if not arg.startswith("-"))
    if not paths:
        fail(f"{compose_file} runs no pytest command")
    return paths


def resolve_test_path(source: Path, path: str, service_root: str | None) -> str:
    """Map a container-relative pytest argument back onto a repo path.

    A file argument stays a file. Folding it up to its parent directory would
    hand the target a claim on every sibling, which pytest does not run.
    """
    candidates = [path] if service_root is None else [path, f"{service_root}/{path}"]
    found = [candidate for candidate in candidates if (ROOT / candidate).exists()]
    if len(found) != 1:
        fail(f"{source} runs pytest on {path}, which does not resolve to one repo path")
    return found[0].rstrip("/")


def makefile_pytest_paths(target: str) -> list[str]:
    """Path arguments of the pytest commands written out in a Makefile target.

    Most integration suites are served by the test-integration-% pattern rule and
    only start their compose file, so they have nothing here. A suite with a rule
    of its own can run pytest on the host as well, and that run is a claim like
    any other. A nested $(MAKE) is not followed: the target it calls starts a
    compose file, which pytest_paths already reads.
    """
    if f"{target}:\n" not in MAKEFILE.read_text():
        return []
    paths: list[str] = []
    for command in make_target_commands(target):
        words = shlex.split(command)
        if "pytest" not in words:
            continue
        arguments = words[words.index("pytest") + 1 :]
        skip_next = False
        for argument in arguments:
            if skip_next:
                skip_next = False
                continue
            if argument.startswith("-"):
                skip_next = argument in PYTEST_FLAGS_WITH_VALUE
                continue
            paths.append(argument)
    return paths


def unit_local_suites() -> list[tuple[str, str]]:
    """The (label, directory) pairs of the ALL_SUITES table in test-unit-local.sh."""
    script = TEST_UNIT_LOCAL.read_text()
    body = script.partition("ALL_SUITES=(")[2].partition("\n)")[0]
    if not body:
        fail("test-unit-local.sh has no ALL_SUITES table")
    suites: list[tuple[str, str]] = []
    for line in body.splitlines():
        entry = line.strip()
        if not entry.startswith('"'):
            continue
        label, _, rest = entry.strip('"').partition("|")
        suites.append((label, rest.partition("|")[0]))
    if not suites:
        fail("test-unit-local.sh ALL_SUITES table is empty")
    return suites


def test_file_patterns() -> tuple[str, ...]:
    """The file-name patterns pytest collects under the root configuration.

    python_files is optional, and pytest falls back to its own defaults when it
    is unset, so the walk has to fall back the same way or it goes blind to half
    the names pytest picks up.
    """
    with (ROOT / "pyproject.toml").open("rb") as f:
        config = tomllib.load(f)
    ini_options = config["tool"]["pytest"]["ini_options"]
    if "python_files" not in ini_options:
        return PYTEST_DEFAULT_PYTHON_FILES
    configured = ini_options["python_files"]
    if isinstance(configured, str):
        return tuple(configured.split())
    return tuple(configured)


def discover_test_files() -> set[str]:
    """Repo-relative files pytest would collect."""
    found: set[str] = set()
    for pattern in test_file_patterns():
        for path in ROOT.rglob(pattern):
            relative = path.relative_to(ROOT)
            if TEST_TREE_SKIP_DIRS.intersection(relative.parts):
                continue
            found.add(str(relative))
    if not found:
        fail("no test directories found in the tree; the walk is broken")
    return found


def discover_test_dirs() -> set[str]:
    """Repo-relative directories holding at least one file pytest would collect."""
    return {str(Path(path).parent) for path in discover_test_files()}


def claiming_target(claims: dict[str, str], test_file: str) -> str | None:
    """The target that runs test_file, or None.

    A directory claim reaches everything under it. A file claim reaches that file
    and stops there.
    """
    if test_file in claims:
        return claims[test_file]
    for parent in Path(test_file).parents:
        if str(parent) in claims:
            return claims[str(parent)]
    return None


def claimed_test_paths(jobs: dict[str, Any]) -> dict[str, str]:
    """Repo path -> the CI target that runs it, read off the targets themselves."""
    claims: dict[str, str] = {}
    for label, test_dir in unit_local_suites():
        if not (ROOT / test_dir).is_dir():
            fail(f"make test-unit suite {label} points at missing directory {test_dir}")
        claims.setdefault(test_dir, f"make test-unit suite {label}")

    for service in matrix_values(require_job(jobs, "test-service"), "service"):
        compose_file = SERVICE_COMPOSE_DIR / f"{service}.yml"
        service_root = SERVICE_COMPOSE_ROOTS.get(service)
        if service_root is None:
            fail(f"service {service} has no /app root declared in SERVICE_COMPOSE_ROOTS")
        for path in pytest_paths(compose_file):
            resolved = resolve_test_path(compose_file, path, service_root)
            claims.setdefault(resolved, f"make test-service SERVICE={service}")

    integration_suites = matrix_values(require_job(jobs, "test-integration"), "suite")
    for suite in integration_suites | set(OUT_OF_PR_INTEGRATION_SUITES):
        target = f"make test-integration-{suite}"
        compose_file = INTEGRATION_COMPOSE_DIR / f"{suite}.yml"
        for path in pytest_paths(compose_file):
            resolved = resolve_test_path(compose_file, path, None)
            claims.setdefault(resolved, target)
        for path in makefile_pytest_paths(f"test-integration-{suite}"):
            resolved = resolve_test_path(MAKEFILE, path, None)
            claims.setdefault(resolved, target)
    return claims


def assert_test_suite_coverage(jobs: dict[str, Any]) -> None:
    claims = claimed_test_paths(jobs)
    test_files = discover_test_files()

    for excluded, reason in UNCLAIMED_TEST_DIRS.items():
        if not (ROOT / excluded).is_dir():
            fail(f"UNCLAIMED_TEST_DIRS names {excluded}, which is not in the tree")
        if not reason.strip():
            fail(f"UNCLAIMED_TEST_DIRS entry {excluded} has no reason")
        target = claiming_target(claims, excluded)
        if target:
            fail(f"{excluded} is both excluded and claimed by {target}")

    for excluded, reason in UNCLAIMED_TEST_FILES.items():
        if excluded not in test_files:
            fail(f"UNCLAIMED_TEST_FILES names {excluded}, which pytest would not collect")
        if not reason.strip():
            fail(f"UNCLAIMED_TEST_FILES entry {excluded} has no reason")
        target = claiming_target(claims, excluded)
        if target:
            fail(f"{excluded} is both excluded and claimed by {target}")

    orphans = []
    for test_file in sorted(test_files):
        if test_file in UNCLAIMED_TEST_FILES:
            continue
        # An exclusion of a directory holds for the files in it, not for its
        # subdirectories, so a new subdirectory of a skipped suite still has to be
        # argued for.
        if str(Path(test_file).parent) in UNCLAIMED_TEST_DIRS:
            continue
        if claiming_target(claims, test_file) is None:
            orphans.append(test_file)
    if orphans:
        fail(
            "test files are run by no CI target: "
            + ", ".join(orphans)
            + ". Add them to a CI target, or to UNCLAIMED_TEST_FILES "
            "(or their directory to UNCLAIMED_TEST_DIRS) with a reason"
        )


BUILD_ARG_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def is_pinned_image(reference: str) -> bool:
    """Whether an image reference names one build.

    A digest is one build by definition. A tag is one build as long as it is not
    latest, which moves. Anything still holding a variable is resolved outside the
    tree, so the tree does not say what gets pulled.
    """
    if "$" in reference:
        return False
    if "@" in reference:
        return True
    tag = reference.rsplit("/", 1)[-1].partition(":")[2]
    return bool(tag) and tag != FLOATING_IMAGE_TAG


def substitute_build_args(reference: str, build_args: dict[str, str]) -> str | None:
    """reference with its build args filled in, or None when the builder supplies one."""
    resolved = reference
    for match in BUILD_ARG_REFERENCE.finditer(reference):
        name = match.group(1) or match.group(2)
        if name not in build_args:
            return None
        resolved = resolved.replace(match.group(0), build_args[name])
    return resolved


def dockerfile_image_references(path: Path) -> list[tuple[int, str]]:
    """(line, image) for every image a Dockerfile builds on.

    Stages of the same file are not images, and neither is a build arg the file
    declares without a default: that value comes from the builder, not from here.
    """
    build_args: dict[str, str] = {}
    stages: set[str] = set()
    references: list[tuple[int, str]] = []

    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        keyword = keyword.upper()
        candidates: list[str] = []

        if keyword == "ARG":
            name, separator, default = rest.strip().partition("=")
            if separator:
                build_args[name.strip()] = default.strip().strip("\"'")
        elif keyword == "FROM":
            words = [word for word in rest.split() if not word.startswith("--")]
            if not words:
                fail(f"{path}:{number} is a FROM without an image")
            image, *alias = words
            candidates.append(image)
            if alias and alias[0].upper() == "AS":
                stages.add(alias[1])
        elif keyword == "COPY":
            candidates.extend(
                word.partition("=")[2] for word in rest.split() if word.startswith("--from=")
            )

        for candidate in candidates:
            if candidate in stages or candidate.isdigit() or candidate == "scratch":
                continue
            resolved = substitute_build_args(candidate, build_args)
            if resolved is None:
                continue
            references.append((number, resolved))
    return references


def mapping_entry(node: yaml.Node, key: str) -> yaml.Node | None:
    """The value node under key, or None when the node is not a mapping with it."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for name, value in node.value:
        if isinstance(name, yaml.ScalarNode) and name.value == key:
            return value
    return None


def repo_path(path: Path) -> str:
    """path relative to the repository, or as given when it sits outside it."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def compose_merge_sources(path: Path, service_name: str, service: yaml.Node) -> list[yaml.Node]:
    """The mappings a service pulls in through the YAML merge key."""
    merge = mapping_entry(service, COMPOSE_MERGE_KEY)
    if merge is None:
        return []
    if isinstance(merge, yaml.MappingNode):
        return [merge]
    if isinstance(merge, yaml.SequenceNode) and all(
        isinstance(item, yaml.MappingNode) for item in merge.value
    ):
        return list(merge.value)
    fail(
        f"{repo_path(path)}: service {service_name} merges something that is not a mapping, "
        "so the image it runs cannot be read"
    )


def compose_service_entry(
    path: Path, service_name: str, service: yaml.Node, key: str
) -> yaml.Node | None:
    """The value node a service has under key, following merge keys.

    A key written on the service wins over one it merges in, which is what a YAML
    merge means, and merges chain, so a merged mapping is searched the same way.
    """
    direct = mapping_entry(service, key)
    if direct is not None:
        return direct
    for merged in compose_merge_sources(path, service_name, service):
        inherited = compose_service_entry(path, service_name, merged, key)
        if inherited is not None:
            return inherited
    return None


def compose_image_references(path: Path) -> list[tuple[int, str]]:
    """(line, image) for every image a compose file pulls, empty for other YAML.

    Whatever this cannot resolve, it fails on. The gate exists to catch a moving tag
    in a file written tomorrow, and a form it silently walks past is worse than no
    check: an image Compose resolves and this does not would pass with no entry in
    UNPINNED_IMAGE_REFS and no reason. So the shapes below are either read exactly or
    named as unreadable, with the file and the service.

    Both halves of a reference come off the same parsed node: the image is the
    scalar's value, the line is that scalar's own mark. Reading the value from the
    parse and then hunting for its line in the raw text missed
    `image: postgres:latest # why`, where the two do not match. Nodes are composed
    rather than constructed, so tags with no constructor (compose's own !reset in
    docker-compose.prod.yml, ansible's !vault) pass through.

    A service with no image is not an unread image: it builds from a Dockerfile,
    which this walk checks on its own, so there is nothing to pin on the service.
    """
    try:
        documents = list(yaml.compose_all(path.read_text(), Loader=yaml.SafeLoader))
    except yaml.YAMLError as error:
        fail(f"{repo_path(path)} does not parse as YAML, so its images cannot be read: {error}")

    references: list[tuple[int, str]] = []
    for document in documents:
        services = mapping_entry(document, "services")
        if not isinstance(services, yaml.MappingNode):
            continue
        for name, service in services.value:
            service_name = name.value if isinstance(name, yaml.ScalarNode) else "<unnamed>"
            if not isinstance(service, yaml.MappingNode):
                fail(
                    f"{repo_path(path)}: service {service_name} is not a mapping, "
                    "so the image it runs cannot be read"
                )
            if compose_service_entry(path, service_name, service, "extends") is not None:
                fail(
                    f"{repo_path(path)}: service {service_name} uses extends, whose image "
                    "this gate does not follow; name the image on the service itself"
                )
            image = compose_service_entry(path, service_name, service, "image")
            if image is None:
                continue
            if not isinstance(image, yaml.ScalarNode):
                fail(
                    f"{repo_path(path)}: service {service_name} has an image that is not a "
                    "single value, so what it pulls cannot be read"
                )
            references.append((image.start_mark.line + 1, image.value))
    return references


def discover_image_references() -> list[tuple[str, int, str]]:
    """(repo path, line, image) for every image reference in the tree."""
    references: list[tuple[str, int, str]] = []
    for directory, subdirectories, files in os.walk(ROOT):
        subdirectories[:] = sorted(
            name for name in subdirectories if name not in IMAGE_FILE_SKIP_DIRS
        )
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(ROOT)
            if any(
                excluded in {str(parent) for parent in relative.parents}
                for excluded in UNPINNED_IMAGE_DIRS
            ):
                continue
            if name == "Dockerfile" or name.startswith("Dockerfile."):
                found = dockerfile_image_references(path)
            elif path.suffix in {".yml", ".yaml"}:
                found = compose_image_references(path)
            else:
                continue
            references.extend((str(relative), number, image) for number, image in found)
    if not references:
        fail("no image references found in the tree; the walk is broken")
    return references


def assert_pinned_base_images() -> None:
    for excluded, reason in UNPINNED_IMAGE_DIRS.items():
        if not (ROOT / excluded).is_dir():
            fail(f"UNPINNED_IMAGE_DIRS names {excluded}, which is not in the tree")
        if not reason.strip():
            fail(f"UNPINNED_IMAGE_DIRS entry {excluded} has no reason")

    floating: list[str] = []
    excused: set[str] = set()
    for path, number, image in discover_image_references():
        if is_pinned_image(image):
            continue
        key = f"{path}::{image}"
        if key in UNPINNED_IMAGE_REFS:
            excused.add(key)
            continue
        floating.append(f"{path}:{number} ({image})")

    for key, reason in UNPINNED_IMAGE_REFS.items():
        if not reason.strip():
            fail(f"UNPINNED_IMAGE_REFS entry {key} has no reason")
        if key not in excused:
            fail(f"UNPINNED_IMAGE_REFS names {key}, which is not a floating image in the tree")

    if floating:
        fail(
            "images are not pinned to a version: "
            + ", ".join(floating)
            + '. Pin an explicit tag or digest, or add "<path>::<image>" to '
            "UNPINNED_IMAGE_REFS with a reason"
        )


def assert_service_tests(jobs: dict[str, Any]) -> None:
    job = require_job(jobs, "test-service")
    if (
        job.get("if")
        != "needs.fast-checks.result == 'success' && needs.ci-contract.result == 'success'"
    ):
        fail("service tests must require fast-checks and ci-contract")
    if matrix_values(job, "service") != compose_suites(SERVICE_COMPOSE_DIR):
        fail("service test matrix does not match tests/compose/service")
    if set(SERVICE_COMPOSE_ROOTS) != compose_suites(SERVICE_COMPOSE_DIR):
        fail("SERVICE_COMPOSE_ROOTS does not match tests/compose/service")
    run_step = step_by_id(job, "service-tests")
    if bounded_command(run_step) != "make test-service SERVICE=${{ matrix.service }}":
        fail("service tests must call make test-service")
    if run_step.get("if") != "matrix.should_run == 'true'":
        fail("service test command must be guarded by matrix.should_run")
    assert_buildx_retry(job)
    assert_step = step_by_name(job, "Assert required service test ran")
    if "steps.service-tests.outcome" not in assert_step.get("run", ""):
        fail("service tests must assert the test step outcome")
    if "always()" not in assert_step.get("if", ""):
        fail("service test assertion must run with always()")
    matrix_text = yaml.dump(job.get("strategy", {}), sort_keys=True)
    for output in ["shared", "packages", "docker-test", "ci", "deps", "integration-tests"]:
        if output_reference(output) not in matrix_text:
            fail(f"service matrix is missing common trigger {output}")


def assert_integration_tests(jobs: dict[str, Any]) -> None:
    job = require_job(jobs, "test-integration")
    if (
        job.get("if")
        != "needs.fast-checks.result == 'success' && needs.ci-contract.result == 'success'"
    ):
        fail("integration tests must require fast-checks and ci-contract")
    expected_suites = compose_suites(INTEGRATION_COMPOSE_DIR) - set(OUT_OF_PR_INTEGRATION_SUITES)
    if matrix_values(job, "suite") != expected_suites:
        fail("integration matrix does not match tests/compose/integration")
    for suite, reason in OUT_OF_PR_INTEGRATION_SUITES.items():
        if not (INTEGRATION_COMPOSE_DIR / f"{suite}.yml").is_file():
            fail(f"OUT_OF_PR_INTEGRATION_SUITES names {suite}, which has no compose file")
        if not reason.strip():
            fail(f"OUT_OF_PR_INTEGRATION_SUITES entry {suite} has no reason")
    job_if = job.get("if", "")
    if "run-integration-tests" in job_if:
        fail("integration tests must not depend on a PR label")
    run_step = step_by_id(job, "integration-tests")
    if bounded_command(run_step) != "make test-integration-${{ matrix.suite }}":
        fail("integration tests must call make test-integration-<suite>")
    assert_buildx_retry(job)
    assert_step = step_by_name(job, "Assert required integration test ran")
    if "steps.integration-tests.outcome" not in assert_step.get("run", ""):
        fail("integration tests must assert the test step outcome")
    matrix_text = yaml.dump(job.get("strategy", {}), sort_keys=True)
    for output in ["shared", "packages", "docker-test", "ci", "deps", "integration-tests"]:
        if output_reference(output) not in matrix_text:
            fail(f"integration matrix is missing common trigger {output}")
    include = job.get("strategy", {}).get("matrix", {}).get("include", [])
    for item in include:
        if not isinstance(item, dict):
            fail("integration matrix contains a non-mapping item")
        if "github.event_name == 'workflow_dispatch'" not in item.get("should_run", ""):
            fail(f"workflow_dispatch does not enable integration suite {item.get('suite')}")
    backend = next(
        (item for item in include if isinstance(item, dict) and item.get("suite") == "backend"),
        None,
    )
    if not isinstance(backend, dict):
        fail("integration matrix is missing backend")
    backend_triggers = backend.get("should_run", "")
    for output in ["api", "langgraph", "shared", "packages", "docker-test", "integration-tests"]:
        if output_reference(output) not in backend_triggers:
            fail(f"backend integration matrix is missing trigger {output}")


def assert_backend_dind_integration(jobs: dict[str, Any]) -> None:
    """The Docker-in-Docker suite is a main-only predecessor in CI's release DAG.

    It remains out of the pull-request matrix on cost grounds. On main, though,
    this job and ``merge-gate`` belong to one workflow graph, so a DinD failure
    physically prevents ``publish-worker-images`` from reaching its marker step.
    """
    job = require_job(jobs, "test-backend-dind-integration")
    if job.get("needs") != ["fast-checks", "ci-contract"]:
        fail("backend Docker-in-Docker job must wait for fast-checks and ci-contract")
    condition = " ".join(str(job.get("if", "")).split())
    for required in (
        "always()",
        "github.event_name == 'push'",
        "github.event_name == 'workflow_dispatch'",
        "github.ref == 'refs/heads/main'",
        "needs.fast-checks.result == 'success'",
        "needs.ci-contract.result == 'success'",
    ):
        if required not in condition:
            fail(f"backend Docker-in-Docker job is missing required condition: {required}")
    if job.get("continue-on-error"):
        fail("backend Docker-in-Docker job must fail its run, not report advisory")
    run_step = step_by_id(job, "integration-tests")
    if bounded_command(run_step) != BACKEND_DIND_COMMAND:
        fail(
            "backend Docker-in-Docker workflow must run the Docker-in-Docker suite "
            "through scripts/ci-infra.sh watch"
        )
    if run_step.get("if"):
        fail("backend Docker-in-Docker test step must not be conditional")
    if run_step.get("continue-on-error"):
        fail("backend Docker-in-Docker test step must fail the job it belongs to")
    assert_step = step_by_name(job, "Assert backend Docker-in-Docker integration test ran")
    if "always()" not in assert_step.get("if", ""):
        fail("backend Docker-in-Docker assertion must run with always()")
    if "steps.integration-tests.outcome" not in assert_step.get("run", ""):
        fail("backend Docker-in-Docker assertion must inspect the test outcome")
    assert_buildx_retry(job)


def assert_service_image_imports(jobs: dict[str, Any]) -> None:
    """Every production service image must import its entry module before merge."""
    job = require_job(jobs, "service-image-imports")
    if job.get("needs") != ["fast-checks", "ci-contract"]:
        fail("service image imports must wait for fast-checks and ci-contract")
    condition = "needs.fast-checks.result == 'success' && needs.ci-contract.result == 'success'"
    if job.get("if") != condition:
        fail("service image imports must require fast-checks and ci-contract")
    python = step_by_name(job, "Set up Python")
    if action_name(python.get("uses", "")) != "actions/setup-python":
        fail("service image imports must set up Python")
    if python.get("with", {}).get("python-version") != "3.12":
        fail("service image imports must use Python 3.12")
    parser = step_by_name(job, "Install Compose and lock parsers")
    if parser.get("run") != "python -m pip install pyyaml==6.0.3 packaging==26.2":
        fail("service image imports must install its pinned Compose and lock parsers")
    assert_buildx_retry(job)
    step = step_by_id(job, "service-image-imports")
    if bounded_command(step) != "python scripts/check_service_image_imports.py":
        fail("service image imports must run the production-image import check")
    if step.get("continue-on-error"):
        fail("service image imports must fail the job they belong to")


def assert_buildx_retry(job: dict[str, Any]) -> None:
    step = step_by_name(job, "Set up Docker Buildx with retry")
    if step.get("uses") != "./.github/actions/setup-buildx-with-retry":
        fail("Docker Buildx setup must use the local retry action")
    for name in SIMULATION_INPUTS:
        if step.get("with", {}).get(name) != f"${{{{ inputs.{name} }}}}":
            fail(f"Docker Buildx setup must receive the workflow-dispatch input {name}")
    if not BUILDX_RETRY_ACTION.is_file():
        fail("Docker Buildx retry action is missing")
    action = yaml.safe_load(BUILDX_RETRY_ACTION.read_text())
    inputs = action.get("inputs", {}) if isinstance(action, dict) else {}
    for name in SIMULATION_INPUTS:
        if name not in inputs or inputs[name].get("default") != "false":
            fail(f"Docker Buildx retry action must support the opt-in simulation {name}")
    steps = action.get("runs", {}).get("steps", []) if isinstance(action, dict) else []
    setup = step_by_name({"steps": steps}, BUILDX_SETUP_STEP)
    if setup.get("run") != BUILDX_SETUP_COMMAND or setup.get("continue-on-error"):
        fail(
            "Docker Buildx setup must bootstrap through scripts/ci-infra.sh retry with a bound "
            "on every attempt, and fail the job when every attempt failed"
        )
    if setup.get("env", {}) != {
        "SIMULATE_REGISTRY_FAILURE": f"${{{{ inputs.{SIMULATED_REGISTRY_FAILURE_INPUT} }}}}",
        "SIMULATE_PULL_HANG": f"${{{{ inputs.{SIMULATED_PULL_HANG_INPUT} }}}}",
    }:
        fail("Docker Buildx setup must hand both simulations to its bootstrap")
    if any(
        action_name(candidate.get("uses", "")) == "docker/setup-buildx-action"
        for candidate in steps
        if isinstance(candidate, dict)
    ):
        fail("docker/setup-buildx-action puts no bound on the buildkit pull; bootstrap instead")
    if not BUILDX_BOOTSTRAP.is_file():
        fail(f"{repo_path(BUILDX_BOOTSTRAP)} is missing")


def action_name(reference: str) -> str:
    """owner/repo[/path] of a uses: reference, without its @ref."""
    return str(reference).partition("@")[0]


def assert_retry_action(path: Path, action: str, step: str, cause: str) -> list[dict[str, Any]]:
    """A local retry action: bounded attempts of action, then the marker.

    Returns the attempt steps. Every attempt continues on error, so the next one
    runs; the last step runs always() and, when no attempt succeeded, writes the
    marker through the helper and fails the job.
    """
    if not path.is_file():
        fail(f"{repo_path(path)} is missing")
    definition = yaml.safe_load(path.read_text())
    steps = definition.get("runs", {}).get("steps", []) if isinstance(definition, dict) else []
    attempts = [
        candidate
        for candidate in steps
        if isinstance(candidate, dict) and action_name(candidate.get("uses", "")) == action
    ]
    if len(attempts) != BUILDX_RETRY_ATTEMPTS:
        fail(f"{repo_path(path)} must make {BUILDX_RETRY_ATTEMPTS} attempts of {action}")
    if not all(attempt.get("continue-on-error") is True for attempt in attempts):
        fail(f"{repo_path(path)} attempts must continue to the next attempt")
    ids = [attempt.get("id") for attempt in attempts]
    if ids != [f"attempt-{number}" for number in range(1, BUILDX_RETRY_ATTEMPTS + 1)]:
        fail(f"{repo_path(path)} attempts must be ids attempt-1..attempt-{BUILDX_RETRY_ATTEMPTS}")
    verify = step_by_name({"steps": steps}, "Fail as CI infrastructure after retry exhaustion")
    if verify is not steps[-1] or verify.get("if") != "always()":
        fail(f"{repo_path(path)} must end with an always() retry-exhaustion step")
    script = verify.get("run", "")
    for attempt_id in ids:
        if f"steps.{attempt_id}.outcome" not in script:
            fail(f"{repo_path(path)} retry exhaustion does not read {attempt_id}")
    mark = f'bash "${{GITHUB_WORKSPACE}}/scripts/ci-infra.sh" mark --step {step} --cause {cause}'
    if mark not in script or not script.rstrip().endswith("exit 1"):
        fail(f"{repo_path(path)} retry exhaustion must write the marker and fail")
    return attempts


def uses_references(path: Path) -> list[tuple[int, str]]:
    """(line, reference) for every uses: in a workflow or action file."""
    try:
        document = yaml.compose(path.read_text(), Loader=yaml.SafeLoader)
    except yaml.YAMLError as error:
        fail(f"{repo_path(path)} does not parse as YAML: {error}")
    references: list[tuple[int, str]] = []
    pending = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if isinstance(key, yaml.ScalarNode) and key.value == "uses":
                    if not isinstance(value, yaml.ScalarNode):
                        fail(f"{repo_path(path)}:{key.start_mark.line + 1} uses is not a value")
                    references.append((value.start_mark.line + 1, value.value))
                else:
                    pending.append(value)
        elif isinstance(node, yaml.SequenceNode):
            pending.extend(node.value)
    return sorted(references)


def local_uses_file(path: Path, number: int, reference: str) -> Path:
    """The file a ./ reference runs: a reusable workflow, or a directory's action.yml."""
    target = ROOT / reference.removeprefix(LOCAL_ACTION_PREFIX)
    if target.suffix in {".yml", ".yaml"} and target.is_file():
        return target
    for name in ("action.yml", "action.yaml"):
        if (target / name).is_file():
            return target / name
    fail(f"{repo_path(path)}:{number} uses {reference}, which is not in the tree")


def assert_pinned_actions(workflow: Path | None = None) -> None:
    """Every third-party action reachable from the workflow is pinned to a commit."""
    pending = [WORKFLOW if workflow is None else workflow]
    seen: set[Path] = set()
    unpinned: list[str] = []
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        lines = path.read_text().splitlines()
        for number, reference in uses_references(path):
            if reference.startswith(LOCAL_ACTION_PREFIX):
                pending.append(local_uses_file(path, number, reference))
                continue
            if not PINNED_ACTION.match(reference) or not ACTION_VERSION_COMMENT.search(
                lines[number - 1]
            ):
                unpinned.append(f"{repo_path(path)}:{number} ({reference})")
    if unpinned:
        fail(
            "third-party actions are not pinned: "
            + ", ".join(unpinned)
            + '. Use owner/repo@<40-character commit SHA> with the tag as a "# vX" comment'
        )


def infra_output_names(job_name: str, job: dict[str, Any]) -> set[str]:
    matrix_key = INFRA_MARKER_JOBS[job_name]
    if matrix_key is None:
        return {INFRA_OUTPUT}
    return {f"{INFRA_OUTPUT}-{value}" for value in matrix_values(job, matrix_key)}


def assert_infra_marker_exposed(jobs: dict[str, Any]) -> None:
    """Each job that can fail on a download hands its marker to merge-gate."""
    helper = CI_INFRA_HELPER.read_text() if CI_INFRA_HELPER.is_file() else ""
    if f'MARKER_PREFIX="{INFRA_MARKER_PREFIX}"' not in helper:
        fail(f"scripts/ci-infra.sh must write the {INFRA_MARKER_PREFIX} marker")
    for job_name, matrix_key in INFRA_MARKER_JOBS.items():
        job = require_job(jobs, job_name)
        steps = job.get("steps", [])
        expose = step_by_name(job, INFRA_EXPOSE_STEP)
        if expose is not steps[-1]:
            fail(f"{job_name} must expose its infrastructure marker as its last step")
        if expose.get("id") != "infra" or expose.get("if") != INFRA_EXPOSE_CONDITION:
            fail(f"{job_name} must expose its marker from step id infra, always()")
        suffix = "" if matrix_key is None else f"-${{{{ matrix.{matrix_key} }}}}"
        if expose.get("run") != f"bash scripts/ci-infra.sh expose --output {INFRA_OUTPUT}{suffix}":
            fail(f"{job_name} exposes its marker under the wrong output")
        if matrix_key is not None:
            leg = job.get("env", {}).get("CI_INFRA_JOB")
            if leg != f"{job_name}/${{{{ matrix.{matrix_key} }}}}":
                fail(f"{job_name} must name its matrix leg in CI_INFRA_JOB")
        expected = {
            name: f"${{{{ steps.infra.outputs['{name}'] }}}}"
            for name in infra_output_names(job_name, job)
        }
        outputs = {
            name: value
            for name, value in job.get("outputs", {}).items()
            if name.startswith(INFRA_OUTPUT)
        }
        if outputs != expected:
            fail(f"{job_name} outputs must be exactly one infrastructure marker per leg")


def assert_download_retries(jobs: dict[str, Any]) -> None:
    """The downloads a job starts with are retried, and name exhaustion as infra."""
    for job_name in ["fast-checks", "ci-contract"]:
        if step_by_name(require_job(jobs, job_name), "Install uv").get("run") != (
            INSTALL_UV_COMMAND
        ):
            fail(f"{job_name} must install uv through scripts/ci-infra.sh retry")
    assert_retry_action(UV_RETRY_ACTION, "astral-sh/setup-uv", "setup-uv", "uv-download")
    for job_name, step_name in UV_SETUP_STEPS.items():
        step = step_by_name(require_job(jobs, job_name), step_name)
        if step.get("uses") != "./.github/actions/setup-uv-with-retry":
            fail(f"{job_name} must set up uv through the local retry action")
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps", []):
            if isinstance(step, dict) and action_name(step.get("uses", "")) == (
                "astral-sh/setup-uv"
            ):
                fail(f"{job_name} must set up uv through the local retry action")
    for job_name, command in PULL_IMAGES_COMMANDS.items():
        job = require_job(jobs, job_name)
        steps = job.get("steps", [])
        pull = step_by_id(job, "pull-images")
        if pull.get("name") != PULL_IMAGES_STEP or pull.get("run") != command:
            fail(f"{job_name} must pre-pull its compose images through scripts/ci-infra.sh")
        if pull.get("continue-on-error"):
            fail(f"{job_name} image pull must fail the job it belongs to")
        tests = step_by_id(
            job, "service-tests" if job_name == "test-service" else "integration-tests"
        )
        if steps.index(pull) > steps.index(tests):
            fail(f"{job_name} must pull its images before running the tests")
        asserts = [
            step
            for step in steps
            if isinstance(step, dict) and str(step.get("name", "")).startswith("Assert ")
        ]
        if not any("steps.pull-images.outcome" in step.get("run", "") for step in asserts):
            fail(f"{job_name} must assert the image pull outcome")


def duration_seconds(duration: str) -> int:
    """A ci-infra.sh duration in seconds: a whole number with an optional s, m or h."""
    match = DURATION.match(duration)
    if match is None or int(match["value"]) == 0:
        fail(f"duration {duration!r} is not a positive N, Ns, Nm or Nh")
    return int(match["value"]) * DURATION_UNIT_SECONDS[match["unit"]]


def bounded_command(step: dict[str, Any]) -> str:
    """The command a step runs under ci-infra.sh bound; fail if it runs unbounded."""
    match = CI_INFRA_BOUND.match(str(step.get("run", "")))
    if match is None:
        fail(f"step {step.get('name')} must run under scripts/ci-infra.sh bound")
    return match["command"]


def ci_infra_constant(name: str) -> str:
    """A constant scripts/ci-infra.sh sets, with an environment override's default."""
    match = re.search(
        rf'^{name}=(?:"\$\{{[A-Z_]+:-(?P<default>[^}}]+)\}}"|(?P<value>\S+))$',
        CI_INFRA_HELPER.read_text(),
        re.MULTILINE,
    )
    if match is None:
        fail(f"scripts/ci-infra.sh does not set {name}")
    return match["default"] or match["value"]


def retry_worst_seconds(attempt_timeout: str) -> int:
    """The longest a bounded ci-infra.sh retry can take: every attempt runs to its bound,
    timeout waits KILL_AFTER before its KILL, and the backoff runs between attempts."""
    attempts = int(ci_infra_constant("RETRY_ATTEMPTS"))
    delay = int(ci_infra_constant("RETRY_DELAY"))
    kill_after = duration_seconds(ci_infra_constant("KILL_AFTER"))
    backoff = sum(delay * attempt for attempt in range(1, attempts))
    return attempts * (duration_seconds(attempt_timeout) + kill_after) + backoff


def external_image_count(compose_file: Path) -> int:
    """How many images ci-infra.sh pull-images pulls for a compose file: the ones a
    service runs without building, and that no other service of the file builds."""
    if not compose_file.is_file():
        fail(f"{repo_path(compose_file)} is missing")
    compose = yaml.safe_load(compose_file.read_text())
    services = compose.get("services", {}) if isinstance(compose, dict) else {}
    built = {service.get("image") for service in services.values() if "build" in service}
    return len(
        {
            service["image"]
            for service in services.values()
            if "build" not in service and service.get("image") and service["image"] not in built
        }
    )


def matrix_legs(job: dict[str, Any]) -> list[dict[str, Any]]:
    include = job.get("strategy", {}).get("matrix", {}).get("include")
    if include is None:
        return [{}]
    if not isinstance(include, list) or not all(isinstance(leg, dict) for leg in include):
        fail("job matrix include is not a list of mappings")
    return include


def literal_matrix_value(value: Any) -> str | None:
    """A matrix value as an `if` compares it, or None when it is itself an expression."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, str)) and "${{" not in str(value):
        return str(value)
    return None


def runs_in_leg(step: dict[str, Any], leg: dict[str, Any]) -> bool:
    """False only when the step's `if` requires a matrix value this leg does not have.

    Anything the check cannot decide from the workflow alone counts as running, so the
    budget errs long, never short.
    """
    condition = step.get("if")
    if not isinstance(condition, str) or "||" in condition:
        return True
    for conjunct in condition.split("&&"):
        match = MATRIX_CONDITION.match(conjunct.strip())
        if match is None or match[1] not in leg:
            continue
        value = literal_matrix_value(leg[match[1]])
        if value is not None and value != match[2]:
            return False
    return True


def render_matrix(text: str, leg: dict[str, Any]) -> str:
    return MATRIX_EXPRESSION.sub(lambda match: str(leg.get(match[1], match[0])), text)


def buildx_attempt_timeout() -> str:
    match = re.search(r"--attempt-timeout (\S+) --", BUILDX_SETUP_COMMAND)
    if match is None:
        fail("the Buildx bootstrap must bound every attempt")
    return match[1]


def step_bound(step: dict[str, Any], leg: dict[str, Any]) -> tuple[int | None, bool]:
    """(the longest the step can take, whether its bound writes a marker).

    The first is None when nothing bounds the step. A GitHub step timeout-minutes bounds
    any step, but when it fires the step is a plain failure: only a ci-infra.sh bound,
    a bounded retry, a pull-images and the Buildx action name what hung.
    """
    bounds: list[int] = []
    marks = False
    minutes = step.get("timeout-minutes")
    if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0:
        bounds.append(minutes * 60)
    run = render_matrix(str(step.get("run", "")), leg)
    if match := CI_INFRA_BOUND.match(run):
        bounds.append(
            duration_seconds(match["timeout"]) + duration_seconds(ci_infra_constant("KILL_AFTER"))
        )
        marks = True
    elif match := CI_INFRA_BOUNDED_RETRY.match(run):
        bounds.append(retry_worst_seconds(match["timeout"]))
        marks = True
    elif match := CI_INFRA_PULL_IMAGES.match(run):
        images = external_image_count(ROOT / match["compose"])
        bounds.append(images * retry_worst_seconds(ci_infra_constant("PULL_ATTEMPT_TIMEOUT")))
        marks = True
    elif step.get("uses") == BUILDX_RETRY_USES:
        bounds.append(retry_worst_seconds(buildx_attempt_timeout()))
        marks = True
    return (min(bounds) if bounds else None), marks


def job_budget_seconds(job_name: str, job: dict[str, Any], leg: dict[str, Any]) -> int | None:
    """The worst case before this leg's always() expose step, or None without a bounded
    step: every step up to the last one whose bound marks, and every always() step after
    it (the steps that are not always() are skipped once a bound has failed the job)."""
    steps = [
        step
        for step in job.get("steps", [])
        if isinstance(step, dict)
        and step.get("name") != INFRA_EXPOSE_STEP
        and runs_in_leg(step, leg)
    ]
    marking = [index for index, step in enumerate(steps) if step_bound(step, leg)[1]]
    if not marking:
        return None
    counted = steps[: marking[-1] + 1] + [
        step for step in steps[marking[-1] + 1 :] if str(step.get("if", "")).startswith("always()")
    ]
    total = JOB_BUDGET_MARGIN_SECONDS
    for step in counted:
        seconds, _ = step_bound(step, leg)
        if seconds is None:
            fail(
                f"{job_name} step {step.get('name')} has no bound, so the worst case of "
                f"{job_name} is unknown: give it a timeout-minutes"
            )
        total += seconds
    return total


def assert_job_timeouts(jobs: dict[str, Any]) -> None:
    """Every job has a timeout-minutes, and the worst case of its bounded steps fits."""
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            fail(f"job {job_name} is not a mapping")
        minutes = job.get("timeout-minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            fail(
                f"{job_name} has no timeout-minutes; without one GitHub waits 360 minutes. "
                "Set one from measured durations with margin"
            )
        if minutes > JOB_TIMEOUT_CEILING_MINUTES:
            fail(
                f"{job_name} timeout-minutes {minutes} is above the "
                f"{JOB_TIMEOUT_CEILING_MINUTES}-minute ceiling"
            )
        for step in job.get("steps", []):
            if not isinstance(step, dict):
                continue
            match = CI_INFRA_BOUND.match(str(step.get("run", "")))
            if match is None:
                continue
            if duration_seconds(match["timeout"]) >= minutes * 60:
                fail(
                    f"{job_name} step {step.get('name')} is bounded at {match['timeout']}, "
                    f"not shorter than the job's {minutes} minutes, so the job limit would "
                    "stop it before it can name the timeout"
                )
        for leg in matrix_legs(job):
            budget = job_budget_seconds(job_name, job, leg)
            if budget is not None and budget > minutes * 60:
                literal = {key: literal_matrix_value(value) for key, value in leg.items()}
                name = job_name + "".join(
                    f" {key}={value}" for key, value in literal.items() if value is not None
                )
                fail(
                    f"{name} can take {budget / 60:g} minutes before its expose step in the "
                    f"worst case (every bounded step and retry attempt at its bound, plus a "
                    f"{JOB_BUDGET_MARGIN_SECONDS // 60}-minute margin), above its "
                    f"timeout-minutes {minutes}: the job limit would stop it before a bound "
                    "could name the hang"
                )
    for job_name, step_names in BOUNDED_DOCKER_STEPS.items():
        job = require_job(jobs, job_name)
        for step_name in step_names:
            bounded_command(step_by_name(job, step_name))
    fast_checks = require_job(jobs, "fast-checks")
    pull = step_by_name(fast_checks, REDIS_PULL_STEP)
    if pull.get("run") != REDIS_PULL_COMMAND:
        fail("fast-checks must pull its Redis image through a bounded scripts/ci-infra.sh retry")
    redis = step_by_name(fast_checks, "Run Redis capability cleanup regression")
    if bounded_command(redis) != REDIS_CLEANUP_COMMAND:
        fail(f"fast-checks must run the Redis regression as {REDIS_CLEANUP_COMMAND}")
    steps = fast_checks.get("steps", [])
    if steps.index(pull) > steps.index(redis):
        fail("fast-checks must pull its Redis image before it runs it")


def assert_gate(jobs: dict[str, Any]) -> None:
    job = require_job(jobs, "merge-gate")
    if job.get("name") != "Required CI Gate":
        fail("merge-gate name must stay Required CI Gate")
    if job.get("if") != "always()":
        fail("merge-gate must run with if: always()")
    needs = set(job.get("needs", []))
    if needs != EXPECTED_GATE_NEEDS:
        fail(f"merge-gate needs mismatch: {sorted(needs)}")
    check_step = step_by_name(job, "Check required jobs")
    script = check_step.get("run", "")
    for need in EXPECTED_GATE_NEEDS:
        if f"needs.{need}.result" not in script:
            fail(f"merge-gate does not inspect {need}")
    if '!= "success"' not in script:
        fail("merge-gate must fail non-success upstream results")
    if check_step.get("env", {}).get("NEEDS_JSON") != "${{ toJSON(needs) }}":
        fail("merge-gate must read every needed job's outputs from toJSON(needs)")
    if INFRA_MARKER_PATTERN not in script:
        fail("merge-gate must repeat every infrastructure marker of its needs")
    if INFRA_MARKER_ANNOTATION not in script or "GITHUB_STEP_SUMMARY" not in script:
        fail("merge-gate must repeat the markers in its log and its summary")
    if script.find(INFRA_MARKER_PATTERN) > script.rfind('echo "Required CI gate failed"'):
        fail("merge-gate must repeat the markers before it exits on its verdict")


def assert_template_compatibility(jobs: dict[str, Any]) -> None:
    job = require_job(jobs, "template-compatibility")
    if job.get("timeout-minutes") != TEMPLATE_COMPAT_TIMEOUT_MINUTES:
        fail("template compatibility job must have a 30 minute timeout")
    if job.get("strategy", {}).get("fail-fast") is not False:
        fail("template compatibility matrix must disable fail-fast")
    if matrix_values(job, "entry") != {"baseline", "candidate"}:
        fail("template compatibility matrix must contain baseline and candidate")
    baseline = step_by_name(job, "Run baseline compatibility smoke")
    if "TEMPLATE_REF" in baseline.get("run", ""):
        fail("baseline must load the production pin from system config")
    candidate = step_by_name(job, "Run candidate compatibility smoke")
    if (
        "CANDIDATE_REF" not in candidate.get("run", "")
        or candidate.get("env", {}).get("CANDIDATE_REF")
        != "${{ inputs.service_template_candidate_ref }}"
    ):
        fail("candidate must accept an explicit workflow input ref")


def main() -> None:
    workflow = load_workflow()
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        fail("workflow has no jobs mapping")
    dispatch_inputs = workflow.get(True, {}).get("workflow_dispatch", {}).get("inputs", {})
    for name in SIMULATION_INPUTS:
        if name not in dispatch_inputs:
            fail(f"workflow_dispatch must expose the Buildx simulation input {name}")
    assert_detect_changes(jobs)
    assert_fast_checks(jobs)
    assert_offline_live_make_target()
    assert_offline_live_unit_runner()
    assert_service_tests(jobs)
    assert_integration_tests(jobs)
    assert_test_suite_coverage(jobs)
    assert_pinned_base_images()
    assert_backend_dind_integration(jobs)
    assert_service_image_imports(jobs)
    assert_template_compatibility(jobs)
    assert_gate(jobs)
    assert_pinned_actions()
    assert_download_retries(jobs)
    assert_infra_marker_exposed(jobs)
    assert_job_timeouts(jobs)
    print("CI gate contract ok")


if __name__ == "__main__":
    main()
