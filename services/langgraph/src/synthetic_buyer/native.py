"""What the platform's own records say the order became, judged without guessing.

Each function reads API records the controller fetched and returns one
observation: its status and a bounded, non-secret detail. A fact the record does
not carry is `unknown`, never assumed; only an explicit contradiction is `failed`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from shared.contracts.dto.capability_preview import CapabilityPlan, CapabilityRoute
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.task import TaskStatus, TaskType
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAOutcome

from .codegen_api import typed_deploy_result, typed_qa_result
from .evidence import ObservationStatus

MODULE_ROUTES = frozenset({CapabilityRoute.MODULE, CapabilityRoute.MODULE_WITH_GLUE})
_TERMINAL_RUNS = frozenset({RunStatus.COMPLETED.value, RunStatus.FAILED.value})


@dataclass(frozen=True)
class Finding:
    status: ObservationStatus
    detail: Any = None


def observed(detail: Any = None) -> Finding:
    return Finding(ObservationStatus.OBSERVED, detail)


def failed(detail: Any = None) -> Finding:
    return Finding(ObservationStatus.FAILED, detail)


def unknown(detail: Any = None) -> Finding:
    return Finding(ObservationStatus.UNKNOWN, detail)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def brief_frozen(brief: dict | None) -> Finding:
    if brief is None:
        return unknown("the story is not backed by a Product Brief")
    detail = {
        "brief_id": brief.get("id"),
        "revision": brief.get("revision"),
        "confirmed_at": brief.get("confirmed_at"),
        "language": (brief.get("content") or {}).get("language"),
    }
    if not brief.get("confirmed_at"):
        return failed(detail)
    return observed(detail)


def plan_routes(plan: CapabilityPlan | None) -> Finding:
    if plan is None:
        return failed("the confirmed brief carries no capability plan: a from-scratch order")
    detail = {
        "preview_id": plan.preview_id,
        "capabilities": [
            {
                "request_id": capability.request_id,
                "capability_id": capability.capability_id,
                "route": capability.route.value,
                "install": None
                if capability.install is None
                else {
                    "package": capability.install.package.name,
                    "version": capability.install.package.version,
                    "core_version": capability.install.core_version,
                    "catalog_digest": capability.install.catalog_digest,
                },
            }
            for capability in plan.capabilities
        ],
    }
    module = [item for item in plan.capabilities if item.route in MODULE_ROUTES]
    if not module or any(item.install is None for item in module):
        return failed(detail)
    return observed(detail)


def preview_after_allowlist(preview: dict | None, allowlist_applied_at: str | None) -> Finding:
    created = parse_time((preview or {}).get("created_at"))
    applied = parse_time(allowlist_applied_at)
    detail = {
        "preview_created_at": (preview or {}).get("created_at"),
        "allowlist": allowlist_applied_at,
    }
    if created is None or applied is None:
        return unknown(detail)
    return observed(detail) if created > applied else failed(detail)


def _task_view(task: dict) -> dict:
    operation = task.get("install_operation") or {}
    return {
        "id": task.get("id"),
        "type": task.get("type"),
        "status": task.get("status"),
        "created_at": task.get("created_at"),
        "blocked_by_task_id": task.get("blocked_by_task_id"),
        "install_operation": {
            key: operation.get(key)
            for key in ("id", "state", "stage", "cycle_started_at", "head_sha", "base_sha")
        }
        if operation
        else None,
    }


def install_mechanical(tasks: list[dict], engineering_runs: list[dict]) -> Finding:
    """Every planned install ran as an API-owned operation, never as model work."""
    installs = [task for task in tasks if task.get("type") == TaskType.INSTALL.value]
    install_ids = {task.get("id") for task in installs}
    model_on_install = sorted(
        run["id"] for run in engineering_runs if run.get("task_id") in install_ids
    )
    detail = {
        "install_tasks": [_task_view(task) for task in installs],
        "engineering_runs_on_install_tasks": model_on_install,
    }
    if not installs or model_on_install:
        return failed(detail)
    for task in installs:
        operation = task.get("install_operation") or {}
        if task.get("status") != TaskStatus.DONE.value or operation.get("state") != "published":
            return failed(detail)
    return observed(detail)


def glue_only(tasks: list[dict]) -> Finding:
    """Every engineering task waits on an install: glue, never the feature from scratch."""
    by_id = {task.get("id"): task for task in tasks}
    work = [task for task in tasks if task.get("type") != TaskType.INSTALL.value]
    unchained = []
    for task in work:
        seen: set[Any] = set()
        current = task.get("blocked_by_task_id")
        reaches_install = False
        while current is not None and current not in seen:
            seen.add(current)
            blocker = by_id.get(current)
            if blocker is None:
                break
            if blocker.get("type") == TaskType.INSTALL.value:
                reaches_install = True
                break
            current = blocker.get("blocked_by_task_id")
        if not reaches_install:
            unchained.append(task.get("id"))
    detail = {"engineering_tasks": [_task_view(task) for task in work], "not_glue": unchained}
    return failed(detail) if unchained else observed(detail)


def product_ci(story: dict) -> Finding:
    timeline = story.get("generated_product_timeline")
    if not isinstance(timeline, dict):
        return unknown("the story holds no generated-product timeline")
    pull_request = timeline.get("pull_request") or {}
    runs = [run for run in timeline.get("ci_runs") or [] if isinstance(run, dict)]
    detail = {
        "pull_request": {
            key: pull_request.get(key)
            for key in ("number", "state", "merged_at", "head_sha", "merge_commit_sha")
        },
        "ci_runs": [
            {key: run.get(key) for key in ("id", "url", "conclusion", "head_sha", "branch")}
            for run in runs
        ],
    }
    if not pull_request.get("merged_at"):
        return unknown(detail)
    green = [
        run
        for run in runs
        if run.get("conclusion") == "success"
        and run.get("head_sha") == pull_request.get("head_sha")
    ]
    return observed(detail) if green else unknown(detail)


def latest_terminal(runs: list[dict]) -> dict | None:
    terminal = [run for run in runs if run.get("status") in _TERMINAL_RUNS]
    return max(terminal, key=lambda run: run.get("completed_at") or "", default=None)


def deploy_success(project_id: str, story_id: str, runs: list[dict]) -> tuple[Finding, dict | None]:
    run = latest_terminal(runs)
    if run is None:
        return unknown("the story has no terminal deploy run"), None
    result = typed_deploy_result(run)
    placed = (result.deployment_result if result is not None else None) or {}
    detail = {
        "run_id": run.get("id"),
        "status": run.get("status"),
        "completed_at": run.get("completed_at"),
        "deploy_outcome": None if result is None else result.deploy_outcome.value,
        "deployed_url": None if result is None else result.deployed_url,
        "application_id": None if result is None else result.application_id,
        "bot_username": None if result is None else result.bot_username,
        # The deployer's own provenance of what it placed: image refs and commit.
        "image_references": placed.get("image_references"),
        "deployed_commit_sha": placed.get("deployed_commit_sha"),
        "skipped_reason": None
        if result is None or result.skipped_reason is None
        else result.skipped_reason.value,
    }
    if str(run.get("project_id")) != project_id or run.get("story_id") != story_id:
        return failed({**detail, "correlation": "wrong project or story"}), None
    if (
        result is None
        or run.get("status") != RunStatus.COMPLETED.value
        or result.deploy_outcome is not DeployOutcome.SUCCESS
        or not result.deployed_url
        or not result.bot_username
    ):
        return failed(detail), None
    return observed(detail), detail


def qa_passed(project_id: str, story_id: str, runs: list[dict], deploy: dict | None) -> Finding:
    run = latest_terminal(runs)
    if run is None:
        return unknown("the story has no terminal QA run")
    result = typed_qa_result(run)
    detail = {
        "run_id": run.get("id"),
        "status": run.get("status"),
        "completed_at": run.get("completed_at"),
        "qa_outcome": None if result is None else result.qa_outcome.value,
        "deployed_url": None if result is None else result.deployed_url,
        "passed_checks": [] if result is None else result.passed_checks[:50],
        "failed_checks": 0 if result is None else len(result.failed_checks),
        "unverified_checks": 0 if result is None else len(result.unverified_checks),
    }
    if str(run.get("project_id")) != project_id or run.get("story_id") != story_id:
        return failed({**detail, "correlation": "wrong project or story"})
    if result is None or result.qa_outcome is not QAOutcome.PASSED:
        return failed(detail)
    if deploy is None:
        return unknown({**detail, "correlation": "no successful deploy to bind to"})
    after = parse_time(run.get("completed_at"))
    deployed = parse_time(deploy.get("completed_at"))
    if result.deployed_url != deploy["deployed_url"] or after is None or deployed is None:
        return failed({**detail, "correlation": "not the story's deployed target"})
    if after < deployed:
        return failed({**detail, "correlation": "QA settled before the deploy"})
    return observed(detail)
