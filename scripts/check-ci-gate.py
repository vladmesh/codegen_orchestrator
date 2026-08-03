#!/usr/bin/env python3
"""Validate the GitHub Actions CI gate contract."""

from __future__ import annotations

from pathlib import Path
import tomllib
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
BACKEND_INTEGRATION_WORKFLOW = ROOT / ".github" / "workflows" / "backend-integration.yml"
BUILDX_RETRY_ACTION = ROOT / ".github" / "actions" / "setup-buildx-with-retry" / "action.yml"
TEST_UNIT_LOCAL = ROOT / "scripts" / "test-unit-local.sh"
MAKEFILE = ROOT / "Makefile"
LINT_PATH_EXPR = "$(if $(LINT_PATH),$(LINT_PATH),.)"

SERVICE_COMPOSE_DIR = ROOT / "docker" / "test" / "service"
INTEGRATION_COMPOSE_DIR = ROOT / "docker" / "test" / "integration"

# Where /app points for each docker/test/service compose file. The pytest paths in
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
MANUAL_ONLY_INTEGRATION_SUITES = {
    "backend-dind": "Docker-in-Docker suite, dispatched by hand via backend-integration.yml",
}

# --- Test suite coverage ----------------------------------------------------
#
# A test directory is any directory in the tree holding at least one file whose
# name matches python_files of the root [tool.pytest.ini_options]; with no such
# setting the patterns are pytest's own defaults, test_*.py and *_test.py. The
# set is walked, never listed by hand, so a directory added tomorrow shows up
# here on its own. It counts as covered when it, or a directory above it (pytest
# recurses into subdirectories), is claimed by a CI target, or when it is named
# exactly in UNCLAIMED_TEST_DIRS.
#
# Claims are read off the targets themselves: the ALL_SUITES table in
# scripts/test-unit-local.sh, and the pytest commands in the compose files behind
# the test-service and test-integration matrices.
PYTEST_DEFAULT_PYTHON_FILES = ("test_*.py", "*_test.py")
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

# Test directories no CI target runs. Every line needs a reason; the point of the
# list is that skipping a suite is a decision on the record, not a default.
UNCLAIMED_TEST_DIRS = {
    "scripts": (
        "scripts/test_e2e_flow.py, test_e2e_analyst.py and e2e_scaffold_test.py "
        "are hand-run drivers against a live stack, invoked through their "
        "__main__ block; the two functions pytest would collect from "
        "test_e2e_flow.py talk to a running API"
    ),
    "tests/e2e": (
        "full-stack e2e behind docker/test/e2e/e2e.yml, which no workflow and no "
        "make target invokes; issue:8a41b0e8a3148a68d6e5"
    ),
    "tests/e2e/mock_anthropic": (
        "the mock LLM server that backs tests/e2e; unreachable for the same reason, "
        "issue:8a41b0e8a3148a68d6e5"
    ),
    "services/langgraph/tests/e2e": (
        "needs a real LLM API key (PO_LLM_API_KEY) and skips without one, so running "
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

EXPECTED_GATE_NEEDS = {
    "detect-changes",
    "fast-checks",
    "ci-contract",
    "test-service",
    "test-integration",
    "template-compatibility",
    "web-checks",
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
OFFLINE_LIVE_IGNORES = {
    "tests/live/test_api_crud.py",
    "tests/live/test_capability_cleanup_redis.py",
    "tests/live/test_ci_prompt.py",
    "tests/live/test_deploy_infra.py",
    "tests/live/test_full_pipeline.py",
    "tests/live/test_health.py",
    "tests/live/test_pipeline_engineering.py",
    "tests/live/test_pipeline_scaffold.py",
    "tests/live/test_scaffold.py",
    "tests/live/test_scaffold_result.py",
    "tests/live/test_streams.py",
    "tests/live/test_supervisor.py",
}
UNIT_TEST_API_BASE_URL = "http://127.0.0.1:9"


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
        "docker/test/**",
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


def resolve_test_path(compose_file: Path, path: str, service_root: str | None) -> str:
    """Map a container-relative pytest argument back onto a repo path."""
    candidates = [path] if service_root is None else [path, f"{service_root}/{path}"]
    found = [candidate for candidate in candidates if (ROOT / candidate).exists()]
    if len(found) != 1:
        fail(f"{compose_file} runs pytest on {path}, which does not resolve to one repo path")
    resolved = found[0].rstrip("/")
    if (ROOT / resolved).is_file():
        resolved = str(Path(resolved).parent)
    return resolved


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


def discover_test_dirs() -> set[str]:
    """Repo-relative directories holding at least one file pytest would collect."""
    found: set[str] = set()
    for pattern in test_file_patterns():
        for path in ROOT.rglob(pattern):
            relative = path.relative_to(ROOT)
            if TEST_TREE_SKIP_DIRS.intersection(relative.parts):
                continue
            found.add(str(relative.parent))
    if not found:
        fail("no test directories found in the tree; the walk is broken")
    return found


def claimed_test_dirs(jobs: dict[str, Any]) -> dict[str, str]:
    """Directory -> the CI target that runs it, read off the targets themselves."""
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
    for suite in integration_suites | set(MANUAL_ONLY_INTEGRATION_SUITES):
        compose_file = INTEGRATION_COMPOSE_DIR / f"{suite}.yml"
        for path in pytest_paths(compose_file):
            resolved = resolve_test_path(compose_file, path, None)
            claims.setdefault(resolved, f"make test-integration-{suite}")
    return claims


def assert_test_suite_coverage(jobs: dict[str, Any]) -> None:
    claims = claimed_test_dirs(jobs)
    for excluded, reason in UNCLAIMED_TEST_DIRS.items():
        if not (ROOT / excluded).is_dir():
            fail(f"UNCLAIMED_TEST_DIRS names {excluded}, which is not in the tree")
        if not reason.strip():
            fail(f"UNCLAIMED_TEST_DIRS entry {excluded} has no reason")
        if excluded in claims:
            fail(f"{excluded} is both excluded and claimed by {claims[excluded]}")

    orphans = []
    for test_dir in sorted(discover_test_dirs()):
        if test_dir in UNCLAIMED_TEST_DIRS:
            continue
        # A claim on a directory carries to what is under it: pytest recurses.
        # An exclusion does not, so a new subdirectory of a skipped suite still
        # has to be argued for.
        parents = {str(parent) for parent in Path(test_dir).parents}
        if not ({test_dir} | parents) & set(claims):
            orphans.append(test_dir)
    if orphans:
        fail(
            "test directories are claimed by no CI target: "
            + ", ".join(orphans)
            + ". Add them to a CI target, or to UNCLAIMED_TEST_DIRS with a reason"
        )


def assert_service_tests(jobs: dict[str, Any]) -> None:
    job = require_job(jobs, "test-service")
    if (
        job.get("if")
        != "needs.fast-checks.result == 'success' && needs.ci-contract.result == 'success'"
    ):
        fail("service tests must require fast-checks and ci-contract")
    if matrix_values(job, "service") != compose_suites(SERVICE_COMPOSE_DIR):
        fail("service test matrix does not match docker/test/service")
    if set(SERVICE_COMPOSE_ROOTS) != compose_suites(SERVICE_COMPOSE_DIR):
        fail("SERVICE_COMPOSE_ROOTS does not match docker/test/service")
    run_step = step_by_id(job, "service-tests")
    if run_step.get("run") != "make test-service SERVICE=${{ matrix.service }}":
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
    expected_suites = compose_suites(INTEGRATION_COMPOSE_DIR) - set(MANUAL_ONLY_INTEGRATION_SUITES)
    if matrix_values(job, "suite") != expected_suites:
        fail("integration matrix does not match docker/test/integration")
    for suite, reason in MANUAL_ONLY_INTEGRATION_SUITES.items():
        if not (INTEGRATION_COMPOSE_DIR / f"{suite}.yml").is_file():
            fail(f"MANUAL_ONLY_INTEGRATION_SUITES names {suite}, which has no compose file")
        if not reason.strip():
            fail(f"MANUAL_ONLY_INTEGRATION_SUITES entry {suite} has no reason")
    job_if = job.get("if", "")
    if "run-integration-tests" in job_if:
        fail("integration tests must not depend on a PR label")
    run_step = step_by_id(job, "integration-tests")
    if run_step.get("run") != "make test-integration-${{ matrix.suite }}":
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


def assert_manual_backend_integration() -> None:
    if not BACKEND_INTEGRATION_WORKFLOW.is_file():
        fail("manual backend integration workflow is missing")
    with BACKEND_INTEGRATION_WORKFLOW.open() as f:
        workflow = yaml.safe_load(f)
    if not isinstance(workflow, dict):
        fail("manual backend integration workflow root is not a mapping")
    if set(workflow.get(True, {})) != {"workflow_dispatch"}:
        fail("backend integration workflow must only run when manually dispatched")
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        fail("manual backend integration workflow has no jobs mapping")
    job = require_job(jobs, "test-backend-dind-integration")
    if job.get("if"):
        fail("manual backend integration job must not be conditional")
    run_step = step_by_id(job, "integration-tests")
    if run_step.get("run") != "make test-integration-backend-dind":
        fail("manual backend integration workflow must run the Docker-in-Docker suite")
    if run_step.get("if"):
        fail("manual backend integration test step must not be conditional")
    assert_step = step_by_name(job, "Assert backend Docker-in-Docker integration test ran")
    if "always()" not in assert_step.get("if", ""):
        fail("manual backend integration assertion must run with always()")
    if "steps.integration-tests.outcome" not in assert_step.get("run", ""):
        fail("manual backend integration assertion must inspect the test outcome")
    if SIMULATED_REGISTRY_FAILURE_INPUT in BACKEND_INTEGRATION_WORKFLOW.read_text():
        fail("registry retry simulation must not skip the manual backend suite")


def assert_buildx_retry(job: dict[str, Any]) -> None:
    step = step_by_name(job, "Set up Docker Buildx with retry")
    if step.get("uses") != "./.github/actions/setup-buildx-with-retry":
        fail("Docker Buildx setup must use the local retry action")
    if step.get("with", {}).get(SIMULATED_REGISTRY_FAILURE_INPUT) != (
        "${{ inputs.simulate_first_attempt_registry_failure }}"
    ):
        fail("Docker Buildx setup must receive the workflow-dispatch failure simulation input")
    if not BUILDX_RETRY_ACTION.is_file():
        fail("Docker Buildx retry action is missing")
    action = yaml.safe_load(BUILDX_RETRY_ACTION.read_text())
    inputs = action.get("inputs", {}) if isinstance(action, dict) else {}
    if SIMULATED_REGISTRY_FAILURE_INPUT not in inputs:
        fail("Docker Buildx retry action must support first-attempt registry failure simulation")
    steps = action.get("runs", {}).get("steps", []) if isinstance(action, dict) else []
    simulation = step_by_name({"steps": steps}, "Simulate unavailable registry on first attempt")
    if simulation.get("if") != f"inputs.{SIMULATED_REGISTRY_FAILURE_INPUT} == 'true'":
        fail("registry failure simulation must be opt-in")
    if simulation.get("continue-on-error") is not True:
        fail("registry failure simulation must allow the retry to continue")
    attempts = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("uses") == "docker/setup-buildx-action@v3"
    ]
    if len(attempts) != BUILDX_RETRY_ATTEMPTS:
        fail("Docker Buildx retry action must make three attempts")
    if not all(step.get("continue-on-error") is True for step in attempts):
        fail("Docker Buildx retry attempts must continue to the next attempt")
    if attempts[0].get("if") != (
        "inputs.simulate_first_attempt_registry_failure != 'true' || "
        "steps.simulate-registry-failure.outcome == 'success'"
    ):
        fail("first Buildx attempt must be replaced by the simulated registry failure")
    verify = step_by_name({"steps": steps}, "Fail as CI infrastructure after retry exhaustion")
    if "Docker image registry" not in verify.get("run", ""):
        fail("Docker Buildx retry exhaustion must identify the registry infrastructure failure")


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
    if SIMULATED_REGISTRY_FAILURE_INPUT not in dispatch_inputs:
        fail("workflow_dispatch must expose the registry failure simulation input")
    assert_detect_changes(jobs)
    assert_fast_checks(jobs)
    assert_offline_live_make_target()
    assert_offline_live_unit_runner()
    assert_service_tests(jobs)
    assert_integration_tests(jobs)
    assert_test_suite_coverage(jobs)
    assert_manual_backend_integration()
    assert_template_compatibility(jobs)
    assert_gate(jobs)
    print("CI gate contract ok")


if __name__ == "__main__":
    main()
