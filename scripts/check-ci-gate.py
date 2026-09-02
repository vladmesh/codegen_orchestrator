#!/usr/bin/env python3
"""Validate the GitHub Actions CI gate contract."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import tomllib
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
BUILDX_RETRY_ACTION = ROOT / ".github" / "actions" / "setup-buildx-with-retry" / "action.yml"
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
    "shared/tests/fixtures/service-template-91e582180b4295bce45155759bdad0dfa43b75f3": (
        "a vendored copy of a service-template release, read by the template "
        "compatibility tests; its compose files belong to that repository, and "
        "editing them here would make the fixture stop matching the release it "
        "fixes. The pins are service-template's to add"
    ),
}

# Single image references left floating on purpose, keyed by "<path>::<image>".
# Same rule again: a reason per line, and a stale entry fails the gate.
UNPINNED_IMAGE_REFS: dict[str, str] = {}

EXPECTED_GATE_NEEDS = {
    "detect-changes",
    "fast-checks",
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
OFFLINE_LIVE_IGNORES = {
    "tests/live/test_api_crud.py",
    "tests/live/test_capability_cleanup_redis.py",
    "tests/live/test_ci_prompt.py",
    "tests/live/test_deploy_infra.py",
    "tests/live/test_full_pipeline.py",
    "tests/live/test_sprint_dod.py",
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
    if run_step.get("run") != "make test-integration-backend-dind":
        fail("backend Docker-in-Docker workflow must run the Docker-in-Docker suite")
    if run_step.get("if"):
        fail("backend Docker-in-Docker test step must not be conditional")
    if run_step.get("continue-on-error"):
        fail("backend Docker-in-Docker test step must fail the job it belongs to")
    assert_step = step_by_name(job, "Assert backend Docker-in-Docker integration test ran")
    if "always()" not in assert_step.get("if", ""):
        fail("backend Docker-in-Docker assertion must run with always()")
    if "steps.integration-tests.outcome" not in assert_step.get("run", ""):
        fail("backend Docker-in-Docker assertion must inspect the test outcome")
    buildx = step_by_id(job, "buildx")
    if buildx.get("with", {}).get(SIMULATED_REGISTRY_FAILURE_INPUT) != (
        "${{ inputs.simulate_first_attempt_registry_failure }}"
    ):
        fail("backend Docker-in-Docker job must receive the Buildx retry simulation input")


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
    assert_pinned_base_images()
    assert_backend_dind_integration(jobs)
    assert_template_compatibility(jobs)
    assert_gate(jobs)
    print("CI gate contract ok")


if __name__ == "__main__":
    main()
