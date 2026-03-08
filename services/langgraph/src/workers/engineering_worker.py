"""Engineering Worker — consumes from jobs:engineering queue.

Run standalone: python -m src.workers.engineering_worker
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..clients.worker_spawner import delete_worker
from ..nodes.resource_allocator import resource_allocator_node
from ._base import start_worker
from ._ci_gate import _wait_for_ci_and_fix
from ._events import publish_callback_event
from ._repo_setup import _create_repo_and_set_secrets


def _parse_telegram_id(user_id: str) -> dict:
    """Build get_project kwargs with telegram_id if user_id is numeric."""
    if user_id and user_id.isdigit():
        return {"telegram_id": int(user_id)}
    return {}


logger = structlog.get_logger(__name__)


async def _update_task_status(
    api, planning_task_id: str, status: str, actor: str = "engineering-worker"
) -> None:
    """Transition a planning-layer task to the given status (best-effort)."""
    try:
        await api.post(
            f"tasks/{planning_task_id}/transition",
            params={"to_status": status},
            json={"actor": actor},
        )
    except Exception:
        logger.warning(
            "task_status_update_failed",
            planning_task_id=planning_task_id,
            target_status=status,
            exc_info=True,
        )


async def _write_task_event(api, planning_task_id: str, event_type: str, details: dict) -> None:
    """Write an event to a planning-layer task (best-effort)."""
    try:
        await api.post(
            f"tasks/{planning_task_id}/events",
            json={
                "event_type": event_type,
                "details": details,
                "actor": "engineering-worker",
            },
        )
    except Exception:
        logger.warning(
            "task_event_write_failed",
            planning_task_id=planning_task_id,
            event_type=event_type,
            exc_info=True,
        )


async def _fail_job(task_id: str, error_msg: str, planning_task_id: str | None = None) -> dict:
    """Mark a run as failed and optionally update planning task."""
    await api_client.patch(f"runs/{task_id}", json={"status": "failed", "error_message": error_msg})
    if planning_task_id:
        await _update_task_status(api_client, planning_task_id, "failed")
    return {"status": "failed", "error": error_msg}


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
    user_id = job_data.get("user_id", "")
    planning_task_id = job_data.get("planning_task_id")

    logger.info(
        "engineering_job_started",
        task_id=task_id,
        project_id=project_id,
        action=action,
    )

    try:
        # Update task status to running
        await api_client.patch(
            f"runs/{task_id}",
            json={"status": "running", "started_at": datetime.now(UTC).isoformat()},
        )

        # Publish progress event
        await publish_callback_event(
            redis,
            callback_stream,
            "progress",
            task_id,
            "Engineering task started",
            user_id=user_id,
            project_id=project_id or "",
        )

        # Fetch project details (with user isolation)
        project = await api_client.get_project(project_id, **_parse_telegram_id(user_id))
        if not project:
            return await _fail_job(task_id, f"Project {project_id} not found", planning_task_id)

        # Fallback: use project config description when queue message has none
        if not description:
            description = (project.get("config") or {}).get("description", "")

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
            await publish_callback_event(
                redis,
                callback_stream,
                "error",
                task_id,
                error_msg,
                user_id=user_id,
                project_id=project_id or "",
            )
            return await _fail_job(task_id, error_msg, planning_task_id)

        # Create repo and set secrets for new project creation on draft projects
        if project_status == "draft" and action == "create":
            await _create_repo_and_set_secrets(project)
            # Refresh in-memory dict — _create_repo_and_set_secrets sets DB status
            # to "scaffolding". Developer node needs to see this to trigger copier.
            project["status"] = ProjectStatus.SCAFFOLDING.value
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
                    f"runs/{task_id}",
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
            "worker_id": None,
            "engineering_status": "idle",
            "iteration_count": 0,
            "test_results": None,
            "needs_human_approval": False,
            "human_approval_reason": None,
            "errors": [],
        }

        # NOTE: Do NOT update project status here.
        # Status must remain "scaffolding" so the developer node's
        # _build_scaffold_config() can detect it and trigger copier.
        # The developer node updates status to "scaffolded" after scaffold
        # succeeds, and engineering_worker sets "developing" after subgraph
        # completes.

        # Create and run engineering subgraph
        engineering_subgraph = create_engineering_subgraph()
        developer_started_at = datetime.now(UTC)
        result = await engineering_subgraph.ainvoke(subgraph_input)

        # Update project status to developing (after scaffold + code generation)
        await api_client.patch(
            f"projects/{project_id}",
            json={"status": ProjectStatus.DEVELOPING.value},
        )

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
                user_id=user_id,
                action=action,
                planning_task_id=planning_task_id,
            )

        else:
            # Blocked, needs approval, or unknown status
            errors = result.get("errors", ["Unknown engineering status"])
            error_msg = "; ".join(errors)
            logger.error("engineering_job_failed_status", task_id=task_id, errors=errors)
            await publish_callback_event(
                redis,
                callback_stream,
                "failed",
                task_id,
                error_msg,
                user_id=user_id,
                project_id=project_id or "",
            )
            return await _fail_job(task_id, error_msg, planning_task_id)

    except Exception as e:
        logger.error(
            "engineering_job_exception",
            task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
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
            user_id=user_id,
            project_id=project_id or "",
        )
        return await _fail_job(task_id, str(e), planning_task_id)


async def _handle_engineering_success(
    result: dict,
    task_id: str,
    project: dict,
    callback_stream: str | None,
    redis: RedisStreamClient,
    skip_deploy: bool,
    developer_started_at: datetime | None = None,
    *,
    user_id: str = "",
    action: str = "create",
    planning_task_id: str | None = None,
) -> dict:
    """Handle successful engineering result: CI gate and auto-deploy."""
    project_id = project["id"]

    # --- commit_sha gate: fail fast if no code was committed ---
    if not result.get("commit_sha"):
        logger.error("no_commit_sha", task_id=task_id, project_id=project_id)
        await api_client.patch(
            f"runs/{task_id}",
            json={
                "status": "failed",
                "error_message": "Developer completed but no commit was made",
            },
        )
        await publish_callback_event(
            redis,
            callback_stream,
            "failed",
            task_id,
            "Development completed but no code was committed",
            user_id=user_id,
            project_id=project_id,
        )
        return {
            "status": "failed",
            "error": "No commit_sha",
            "finished_at": datetime.now(UTC).isoformat(),
        }

    logger.info(
        "engineering_job_success",
        task_id=task_id,
        commit_sha=result.get("commit_sha"),
    )

    # --- Refresh project before CI check ---
    fresh_project = await api_client.get_project(project_id, **_parse_telegram_id(user_id))
    if fresh_project:
        project = fresh_project

    # Resolve git_url from primary Repository entity
    primary_repo = await api_client.get_primary_repository(project_id)
    _git_url = primary_repo.get("git_url", "") if primary_repo else ""

    # --- CI Gate: wait for ci.yml before triggering deploy ---
    worker_id = result.get("worker_id")
    try:
        ci_passed, ci_attempts = await _wait_for_ci_and_fix(
            project=project,
            git_url=_git_url,
            task_id=task_id,
            callback_stream=callback_stream,
            redis=redis,
            developer_started_at=developer_started_at,
            user_id=user_id,
            worker_id=worker_id,
            commit_sha=result.get("commit_sha"),
        )
    finally:
        # Cleanup: delete worker container after CI gate (regardless of outcome)
        if worker_id:
            try:
                await delete_worker(worker_id, reason="completed")
                logger.info("worker_deleted_after_ci_gate", worker_id=worker_id)
            except Exception as e:
                logger.warning("worker_delete_failed", worker_id=worker_id, error=str(e))

    failed_count = sum(1 for a in ci_attempts if a["status"] == "failed")

    if not ci_passed:
        fail_msg = f"CI failed after {len(ci_attempts)} attempt(s), retries exhausted"
        logger.error("ci_gate_failed", task_id=task_id, project_id=project_id)
        if planning_task_id:
            await _update_task_status(api_client, planning_task_id, "failed")
        await api_client.patch(
            f"runs/{task_id}",
            json={
                "status": "failed",
                "error_message": fail_msg,
            },
        )
        await publish_callback_event(
            redis,
            callback_stream,
            "failed",
            task_id,
            fail_msg,
            user_id=user_id,
            project_id=project_id,
        )
        return {
            "status": "failed",
            "error": fail_msg,
            "finished_at": datetime.now(UTC).isoformat(),
        }

    # CI passed — mark engineering task as completed
    await api_client.patch(
        f"runs/{task_id}",
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

    # Update planning-layer task if linked
    if planning_task_id:
        await _update_task_status(api_client, planning_task_id, "done")
        await _write_task_event(
            api_client,
            planning_task_id,
            "iteration_end",
            {
                "commit_sha": result.get("commit_sha"),
                "ci_result": "passed",
                "summary": f"Engineering run {task_id} completed",
            },
        )

    ci_summary = "CI passed"
    if failed_count:
        ci_summary = f"CI passed after {failed_count} failed attempt(s)"

    # When planning_task_id is set, skip deploy (dispatcher handles it on story complete)
    effective_skip_deploy = skip_deploy or bool(planning_task_id)

    if effective_skip_deploy:
        # This IS the final step — tell user we're done
        await publish_callback_event(
            redis,
            callback_stream,
            "completed",
            task_id,
            f"Engineering task completed, {ci_summary}",
            user_id=user_id,
            project_id=project_id,
        )
    else:
        # Deploy is next — only send progress, deploy worker sends "completed" on success
        await publish_callback_event(
            redis,
            callback_stream,
            "progress",
            task_id,
            f"{ci_summary}, deploying...",
            user_id=user_id,
            project_id=project_id,
        )

    # Auto-trigger deploy after CI passes (unless skip_deploy or task-linked)
    if not effective_skip_deploy:
        deploy_task_id = f"deploy-{task_id.replace('eng-', '')}"
        try:
            # Create deploy task in API
            await api_client.post(
                "runs/",
                json={
                    "id": deploy_task_id,
                    "type": RunType.DEPLOY.value,
                    "project_id": project_id,
                    "status": RunStatus.QUEUED.value,
                },
            )
            # Queue deploy job
            deploy_msg = DeployMessage(
                task_id=deploy_task_id,
                project_id=project_id,
                user_id=user_id,
                callback_stream=callback_stream,
                triggered_by=DeployTrigger.ENGINEERING,
                action=action,
            )
            await redis.redis.xadd(
                DEPLOY_QUEUE,
                {"data": deploy_msg.model_dump_json()},
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
            await publish_callback_event(
                redis,
                callback_stream,
                "failed",
                task_id,
                f"CI passed but deploy trigger failed: {e}",
                user_id=user_id,
                project_id=project_id,
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
