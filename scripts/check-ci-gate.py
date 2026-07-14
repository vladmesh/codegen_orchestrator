#!/usr/bin/env python3
"""Validate the GitHub Actions CI gate contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

EXPECTED_SERVICE_MATRIX = {
    "api",
    "langgraph",
    "scheduler",
    "telegram_bot",
    "worker-manager",
    "infra",
}
EXPECTED_INTEGRATION_MATRIX = {"backend", "template", "frontend", "infra", "po-tools"}
EXPECTED_GATE_NEEDS = {
    "detect-changes",
    "fast-checks",
    "ci-contract",
    "test-service",
    "test-integration",
    "template-compatibility",
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
}
HYPHENATED_OUTPUTS = {"worker-manager", "infra-service", "docker-test", "integration-tests"}
TEMPLATE_COMPAT_TIMEOUT_MINUTES = 30


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


def assert_service_tests(jobs: dict[str, Any]) -> None:
    job = require_job(jobs, "test-service")
    if (
        job.get("if")
        != "needs.fast-checks.result == 'success' && needs.ci-contract.result == 'success'"
    ):
        fail("service tests must require fast-checks and ci-contract")
    if matrix_values(job, "service") != EXPECTED_SERVICE_MATRIX:
        fail("service test matrix does not match docker/test/service")
    run_step = step_by_id(job, "service-tests")
    if run_step.get("run") != "make test-service SERVICE=${{ matrix.service }}":
        fail("service tests must call make test-service")
    if run_step.get("if") != "matrix.should_run == 'true'":
        fail("service test command must be guarded by matrix.should_run")
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
    if matrix_values(job, "suite") != EXPECTED_INTEGRATION_MATRIX:
        fail("integration matrix does not match docker/test/integration")
    job_if = job.get("if", "")
    if "run-integration-tests" in job_if:
        fail("integration tests must not depend on a PR label")
    run_step = step_by_id(job, "integration-tests")
    if run_step.get("run") != "make test-integration-${{ matrix.suite }}":
        fail("integration tests must call make test-integration-<suite>")
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
        if item.get("suite") == "backend":
            should_run = item.get("should_run", "")
            if "needs.detect-changes.outputs" in should_run:
                fail("backend integration suite must stay workflow_dispatch-only")


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
    assert_detect_changes(jobs)
    assert_fast_checks(jobs)
    assert_service_tests(jobs)
    assert_integration_tests(jobs)
    assert_template_compatibility(jobs)
    assert_gate(jobs)
    print("CI gate contract ok")


if __name__ == "__main__":
    main()
