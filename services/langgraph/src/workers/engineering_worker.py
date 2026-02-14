"""Engineering Worker — consumes from jobs:engineering queue.

Run standalone: python -m src.workers.engineering_worker
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import re
from typing import TYPE_CHECKING
import uuid

import structlog

if TYPE_CHECKING:
    from shared.clients.github import GitHubAppClient

from shared.contracts.dto.project import ProjectStatus, ServiceModule
from shared.contracts.queues.scaffolder import ScaffolderMessage
from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE, SCAFFOLDER_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..nodes.resource_allocator import resource_allocator_node
from ._base import start_worker
from ._events import publish_callback_event

logger = structlog.get_logger(__name__)


def _extract_run_id_from_error(error_msg: str) -> int | None:
    """Extract workflow run ID from URL in RuntimeError message.

    The wait_for_workflow_completion raises RuntimeError with format:
    'Workflow ci.yml failed: failure. See: https://github.com/.../actions/runs/12345'
    """
    match = re.search(r"/actions/runs/(\d+)", error_msg)
    return int(match.group(1)) if match else None


async def _respawn_developer_for_ci_fix(
    project: dict,
    owner: str,
    repo_name: str,
    repo_full_name: str,
    github_client: GitHubAppClient,
    failure_context: str,
    attempt: int,
) -> bool:
    """Spawn a new developer worker to fix CI failures.

    Returns:
        True if developer completed successfully, False otherwise
    """
    from ..clients.worker_spawner import request_spawn
    from ..config.constants import Timeouts

    access_token = await github_client.get_token(owner, repo_name)
    project_name = project.get("name", "project")

    task_message = f"""# Task: Fix CI Failures (Attempt {attempt})

## Context

The code was pushed but CI failed. Your job is to fix the issues and push again.

## CI Failure Details

{failure_context or "CI workflow failed. Run `ruff check .` and fix any linting errors."}

## Instructions

1. The repository is already cloned to `/workspace`. Pull latest changes with `git pull`.
2. Run `ruff check .` to see current linting errors
3. Run `ruff format --exclude 'services/**/migrations' --exclude '.venv' .` to auto-format
4. Run `ruff check --fix --exclude 'services/**/migrations' --exclude '.venv' .` to auto-fix
5. For remaining errors that can't be auto-fixed, manually fix them
6. Commit and push your fixes

## Important

- Focus ONLY on fixing the CI failures, do not add new features
- Make a descriptive commit message like "fix: resolve CI linting errors"
"""

    worker_result = await request_spawn(
        repo=repo_full_name,
        github_token=access_token,
        task_content=task_message,
        task_title=f"Fix CI failures for {project_name} (attempt {attempt})",
        timeout_seconds=Timeouts.WORKER_SPAWN,
    )

    return worker_result.success


async def _wait_for_ci_and_fix(
    project: dict,
    task_id: str,
    callback_stream: str | None,
    redis: RedisStreamClient,
    developer_started_at: datetime | None = None,
) -> bool:
    """Wait for CI workflow to pass, re-spawning developer on failure.

    After the developer pushes code, CI runs on GitHub. This function monitors
    the CI workflow and, if it fails, re-spawns a developer worker to fix
    the issues. Retries up to CI.MAX_FIX_RETRIES times.

    Returns:
        True if CI passed (proceed to deploy), False if max retries exhausted
    """
    from shared.clients.github import GitHubAppClient

    from ..config.constants import CI

    repo_url = project.get("repository_url", "")
    if not repo_url or "github.com/" not in repo_url:
        logger.warning("ci_check_skip_no_repo_url", task_id=task_id)
        return True  # Can't check CI without repo URL; proceed anyway

    repo_full_name = repo_url.split("github.com/")[-1].rstrip("/")
    owner, repo_name = repo_full_name.split("/", 1)

    github_client = GitHubAppClient()

    for attempt in range(CI.MAX_FIX_RETRIES + 1):  # 0 = initial check, 1..N = retries
        # attempt 0: use pre-developer timestamp (CI was triggered during dev)
        # attempt 1+: use fresh timestamp (CI triggered by respawned fix worker)
        if attempt == 0 and developer_started_at:
            created_after = developer_started_at
        else:
            created_after = datetime.now(UTC)

        try:
            logger.info(
                "ci_check_waiting",
                task_id=task_id,
                attempt=attempt,
                workflow=CI.CI_WORKFLOW_FILE,
            )

            msg = "Waiting for CI checks..."
            if attempt > 0:
                msg = f"Waiting for CI checks (retry {attempt})..."
            await publish_callback_event(redis, callback_stream, "progress", task_id, msg)

            run_info = await github_client.wait_for_workflow_completion(
                owner=owner,
                repo=repo_name,
                workflow_file=CI.CI_WORKFLOW_FILE,
                branch="main",
                timeout_seconds=CI.WORKFLOW_TIMEOUT,
                poll_interval=CI.POLL_INTERVAL,
                created_after=created_after,
            )

            logger.info(
                "ci_check_passed",
                task_id=task_id,
                attempt=attempt,
                run_id=run_info["id"],
            )
            return True

        except RuntimeError as e:
            # CI failed
            run_id = _extract_run_id_from_error(str(e))
            logger.warning(
                "ci_check_failed",
                task_id=task_id,
                attempt=attempt,
                error=str(e),
            )

            if attempt >= CI.MAX_FIX_RETRIES:
                logger.error(
                    "ci_fix_retries_exhausted",
                    task_id=task_id,
                    max_retries=CI.MAX_FIX_RETRIES,
                )
                return False

            # Fetch failure details for context
            failure_context = ""
            if run_id:
                try:
                    failure_context = await github_client.get_workflow_failure_logs(
                        owner, repo_name, run_id
                    )
                except Exception as log_err:
                    logger.warning("ci_log_fetch_failed", error=str(log_err))

            # Re-spawn developer worker with fix context
            logger.info(
                "ci_fix_respawn_developer",
                task_id=task_id,
                attempt=attempt + 1,
            )

            fix_success = await _respawn_developer_for_ci_fix(
                project=project,
                owner=owner,
                repo_name=repo_name,
                repo_full_name=repo_full_name,
                github_client=github_client,
                failure_context=failure_context,
                attempt=attempt + 1,
            )

            if not fix_success:
                logger.error(
                    "ci_fix_developer_failed",
                    task_id=task_id,
                    attempt=attempt + 1,
                )
                return False

            # Loop continues: will wait for CI again

        except TimeoutError:
            logger.error("ci_check_timeout", task_id=task_id, attempt=attempt)
            return False

    return False


async def _trigger_scaffolding(project: dict, redis: RedisStreamClient) -> None:
    """Trigger scaffolding for a project in draft status.

    Sends ScaffolderMessage to scaffolder:queue and updates project status.
    """
    project_id = project["id"]
    project_name = project.get("name", project_id)

    # Get GITHUB_ORG for repo name
    org_name = os.getenv("GITHUB_ORG")
    if not org_name:
        raise RuntimeError("GITHUB_ORG environment variable is not set")

    # Generate repo name from project name
    repo_name = project_name.lower().replace(" ", "-").replace("_", "-")
    repo_name = re.sub(r"[^a-z0-9-]", "", repo_name)
    repo_name = re.sub(r"-+", "-", repo_name).strip("-")
    if not repo_name:
        repo_name = project_id[:8]
    repo_full_name = f"{org_name}/{repo_name}"

    # Get modules from project config
    project_config = project.get("config") or {}
    modules_list = project_config.get("modules", ["backend"])

    # Convert to ServiceModule enum
    service_modules = []
    for mod in modules_list:
        try:
            service_modules.append(ServiceModule(mod))
        except ValueError:
            logger.warning("unknown_module_skipped", module=mod)
    if not service_modules:
        service_modules = [ServiceModule.BACKEND]

    # Get task description
    task_description = project_config.get("description", "")
    if not task_description:
        task_description = project_config.get("detailed_spec", "")

    # Build scaffolder message
    scaffolder_msg = ScaffolderMessage(
        request_id=str(uuid.uuid4()),
        project_id=project_id,
        project_name=project_name,
        repo_full_name=repo_full_name,
        modules=service_modules,
        task_description=task_description,
    )

    # Send to scaffolder queue
    await redis.redis.xadd(
        SCAFFOLDER_QUEUE,
        {"data": scaffolder_msg.model_dump_json()},
    )

    # Update project status to scaffolding
    await api_client.patch(
        f"projects/{project_id}",
        json={"status": ProjectStatus.SCAFFOLDING.value},
    )

    logger.info(
        "scaffolding_triggered",
        project_id=project_id,
        repo_full_name=repo_full_name,
        modules=[m.value for m in service_modules],
    )


async def process_engineering_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single engineering job by running Engineering Subgraph.

    Args:
        job_data: Job data from Redis queue (task_id, project_id, user_id, callback_stream)
        redis: Redis client for publishing events

    Returns:
        Result dict with status and details
    """
    from ..subgraphs.engineering import create_engineering_subgraph

    task_id = job_data.get("task_id", "unknown")
    project_id = job_data.get("project_id")
    callback_stream = job_data.get("callback_stream")
    action = job_data.get("action", "create")
    description = job_data.get("description")
    skip_deploy = job_data.get("skip_deploy", False)

    logger.info(
        "engineering_job_started",
        task_id=task_id,
        project_id=project_id,
        action=action,
    )

    try:
        # Update task status to running
        await api_client.patch(f"tasks/{task_id}", json={"status": "running"})

        # Publish progress event
        await publish_callback_event(
            redis, callback_stream, "progress", task_id, "Engineering task started"
        )

        # Fetch project details
        project = await api_client.get_project(project_id)
        if not project:
            error_msg = f"Project {project_id} not found"
            await api_client.patch(
                f"tasks/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )
            return {"status": "failed", "error": error_msg}

        # Fail fast if scaffold previously failed
        project_status = project.get("status")
        if project_status == "scaffold_failed":
            error_msg = (
                f"Project {project_id} has status 'scaffold_failed'. "
                "Scaffold must succeed before developer can work. "
                "Fix the scaffolding issue and retry."
            )
            logger.error(
                "scaffold_failed_abort",
                task_id=task_id,
                project_id=project_id,
                action=action,
            )
            await api_client.patch(
                f"tasks/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )
            await publish_callback_event(redis, callback_stream, "error", task_id, error_msg)
            return {"status": "failed", "error": error_msg}

        # Trigger scaffolding only for new project creation on draft projects
        if project_status == "draft" and action == "create":
            await _trigger_scaffolding(project, redis)
        elif project_status == "draft" and action != "create":
            logger.warning(
                "feature_fix_on_draft_project",
                task_id=task_id,
                project_id=project_id,
                action=action,
                hint="Project is in draft status but action is not 'create'. "
                "Skipping scaffolding — developer will work with existing repo.",
            )

        # Allocate resources if not already allocated
        existing_allocations = await api_client.get_project_allocations(project_id)
        if existing_allocations:
            # Convert existing allocations to allocated_resources format
            allocated_resources = {
                f"{a['server_handle']}:{a['port']}": a for a in existing_allocations
            }
            logger.info(
                "using_existing_allocations",
                task_id=task_id,
                project_id=project_id,
                count=len(allocated_resources),
            )
        else:
            # Run resource allocator to create allocations
            logger.info(
                "allocating_resources",
                task_id=task_id,
                project_id=project_id,
            )
            alloc_result = await resource_allocator_node.run(
                {
                    "project_id": project_id,
                    "project_spec": project,
                    "allocated_resources": {},
                    "errors": [],
                }
            )

            if alloc_result.get("errors"):
                error_msg = "; ".join(alloc_result["errors"])
                logger.error(
                    "resource_allocation_failed",
                    task_id=task_id,
                    project_id=project_id,
                    errors=alloc_result["errors"],
                )
                await api_client.patch(
                    f"tasks/{task_id}",
                    json={"status": "failed", "error_message": error_msg},
                )
                return {"status": "failed", "error": error_msg}

            allocated_resources = alloc_result.get("allocated_resources", {})
            logger.info(
                "resources_allocated",
                task_id=task_id,
                project_id=project_id,
                count=len(allocated_resources),
            )

        # Prepare EngineeringState
        subgraph_input = {
            "messages": [],
            "current_project": project_id,
            "project_spec": project,
            "allocated_resources": allocated_resources,
            "action": action,
            "description": description,
            "commit_sha": None,
            "engineering_status": "idle",
            "iteration_count": 0,
            "test_results": None,
            "needs_human_approval": False,
            "human_approval_reason": None,
            "errors": [],
        }

        # Update project status to developing
        await api_client.patch(
            f"projects/{project_id}",
            json={"status": ProjectStatus.DEVELOPING.value},
        )

        # Create and run engineering subgraph
        engineering_subgraph = create_engineering_subgraph()
        developer_started_at = datetime.now(UTC)
        result = await engineering_subgraph.ainvoke(subgraph_input)

        # Check result status
        if result.get("engineering_status") == "done":
            logger.info(
                "engineering_job_success",
                task_id=task_id,
                commit_sha=result.get("commit_sha"),
            )

            # --- CI Gate & Auto-Deploy ---
            return await _handle_engineering_success(
                result=result,
                task_id=task_id,
                project=project,
                callback_stream=callback_stream,
                redis=redis,
                skip_deploy=skip_deploy,
                developer_started_at=developer_started_at,
            )

        elif result.get("engineering_status") == "blocked" or result.get("needs_human_approval"):
            logger.info("engineering_job_blocked", task_id=task_id, errors=result.get("errors"))
            await api_client.patch(
                f"tasks/{task_id}",
                json={
                    "status": "failed",
                    "error_message": "; ".join(result.get("errors", ["Task blocked"])),
                },
            )

            await publish_callback_event(
                redis,
                callback_stream,
                "failed",
                task_id,
                "Engineering task blocked or needs approval",
            )

            return {
                "status": "failed",
                "error": "; ".join(result.get("errors", ["Task blocked"])),
                "finished_at": datetime.now(UTC).isoformat(),
            }

        else:
            # Unknown status
            errors = result.get("errors", ["Unknown engineering status"])
            logger.error("engineering_job_unknown_status", task_id=task_id, errors=errors)
            await api_client.patch(
                f"tasks/{task_id}",
                json={"status": "failed", "error_message": "; ".join(errors)},
            )
            return {
                "status": "failed",
                "error": "; ".join(errors),
                "finished_at": datetime.now(UTC).isoformat(),
            }

    except Exception as e:
        logger.error(
            "engineering_job_exception",
            task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        await api_client.patch(
            f"tasks/{task_id}",
            json={"status": "failed", "error_message": str(e), "error_traceback": str(e)},
        )
        # Update project status to failed
        if project_id:
            await api_client.patch(
                f"projects/{project_id}",
                json={"status": ProjectStatus.FAILED.value},
            )

        await publish_callback_event(
            redis,
            callback_stream,
            "failed",
            task_id,
            f"Engineering task failed: {e!s}",
        )

        return {
            "status": "failed",
            "error": str(e),
            "finished_at": datetime.now(UTC).isoformat(),
        }


async def _handle_engineering_success(
    result: dict,
    task_id: str,
    project: dict,
    callback_stream: str | None,
    redis: RedisStreamClient,
    skip_deploy: bool,
    developer_started_at: datetime | None = None,
) -> dict:
    """Handle successful engineering result: CI gate and auto-deploy."""
    logger.info(
        "engineering_job_success",
        task_id=task_id,
        commit_sha=result.get("commit_sha"),
    )

    project_id = project["id"]

    # --- CI Gate: wait for ci.yml before triggering deploy ---
    ci_passed = await _wait_for_ci_and_fix(
        project=project,
        task_id=task_id,
        callback_stream=callback_stream,
        redis=redis,
        developer_started_at=developer_started_at,
    )

    if not ci_passed:
        logger.error("ci_gate_failed", task_id=task_id, project_id=project_id)
        await api_client.patch(
            f"tasks/{task_id}",
            json={
                "status": "failed",
                "error_message": "CI checks failed after max retries",
            },
        )
        await publish_callback_event(
            redis,
            callback_stream,
            "failed",
            task_id,
            "Engineering completed but CI checks failed",
        )
        return {
            "status": "failed",
            "error": "CI checks failed after max retries",
            "finished_at": datetime.now(UTC).isoformat(),
        }

    # CI passed — mark engineering task as completed
    await api_client.patch(
        f"tasks/{task_id}",
        json={
            "status": "completed",
            "result": {
                "engineering_status": result["engineering_status"],
                "commit_sha": result.get("commit_sha"),
                "selected_modules": result.get("selected_modules"),
                "test_results": result.get("test_results"),
            },
        },
    )

    await publish_callback_event(
        redis,
        callback_stream,
        "completed",
        task_id,
        "Engineering task completed, CI passed",
    )

    # Auto-trigger deploy after CI passes (unless skip_deploy)
    if not skip_deploy:
        deploy_task_id = f"deploy-{task_id.replace('eng-', '')}"
        try:
            # Create deploy task in API
            await api_client.post(
                "tasks/",
                json={
                    "id": deploy_task_id,
                    "type": "deploy",
                    "project_id": project_id,
                    "status": "pending",
                },
            )
            # Queue deploy job
            await redis.redis.xadd(
                DEPLOY_QUEUE,
                {
                    "data": json.dumps(
                        {
                            "task_id": deploy_task_id,
                            "project_id": project_id,
                            "callback_stream": callback_stream,
                        }
                    )
                },
            )
            logger.info(
                "deploy_auto_triggered",
                task_id=task_id,
                deploy_task_id=deploy_task_id,
                project_id=project_id,
            )
        except Exception as e:
            logger.error(
                "deploy_auto_trigger_failed",
                task_id=task_id,
                error=str(e),
            )
    else:
        deploy_task_id = None
        logger.info(
            "deploy_skipped",
            task_id=task_id,
            project_id=project_id,
        )

    return {
        "status": "success",
        "commit_sha": result.get("commit_sha"),
        "deploy_task_id": deploy_task_id,
        "finished_at": datetime.now(UTC).isoformat(),
    }


def main():
    """Entry point for running as module."""
    start_worker(
        service_name="engineering-worker",
        queue=ENGINEERING_QUEUE,
        process_fn=process_engineering_job,
    )


if __name__ == "__main__":
    main()
