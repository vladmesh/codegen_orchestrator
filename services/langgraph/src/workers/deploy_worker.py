"""Deploy Worker — consumes from jobs:deploy queue and runs DevOps.

Run standalone: python -m src.workers.deploy_worker
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.queues.deploy import DeployMessage
from shared.queues import DEPLOY_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..schemas.api_types import ProjectInfo
from ..subgraphs.devops import create_devops_subgraph
from ._base import start_worker
from ._events import publish_callback_event, publish_proactive_message

logger = structlog.get_logger(__name__)


async def _check_duplicate_deploy(task_id: str, project_id: str) -> dict | None:
    """Check if another deploy is already running or queued for this project.

    Returns cancel result dict if duplicate found, None otherwise.
    """
    # API only supports single status filter, so check both running and queued
    for check_status in (RunStatus.RUNNING, RunStatus.QUEUED):
        existing = await api_client.get(
            "runs/",
            params={
                "project_id": project_id,
                "task_type": RunType.DEPLOY.value,
                "status": check_status.value,
            },
        )
        # Filter out self (current task may already be queued)
        existing = [t for t in existing if t["id"] != task_id]
        if existing:
            existing_id = existing[0]["id"]
            logger.info(
                "deploy_skipped_duplicate",
                task_id=task_id,
                project_id=project_id,
                existing_task_id=existing_id,
                existing_status=check_status.value,
            )
            await api_client.patch(
                f"runs/{task_id}",
                json={
                    "status": RunStatus.CANCELLED.value,
                    "error_message": (
                        f"Skipped: deploy {existing_id} is already"
                        f" {check_status.value} for this project"
                    ),
                },
            )
            return {"status": "cancelled", "existing_task_id": existing_id}

    return None


async def _handle_smoke_failure(
    *,
    result: dict,
    smoke_result: dict,
    task_id: str,
    project_id: str,
    project_name: str,
    callback_stream: str,
    user_id: str,
    redis: RedisStreamClient,
) -> dict:
    """Handle deploy success with smoke test failure."""
    smoke_details = "; ".join(
        f"{c['module']}: {c['detail']}"
        for c in smoke_result.get("checks", [])
        if c.get("result") == "fail"
    )
    error_msg = f"Deployed but smoke test failed: {smoke_details}"
    logger.warning(
        "deploy_job_smoke_failed",
        task_id=task_id,
        deployed_url=result["deployed_url"],
        smoke_details=smoke_details,
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": "failed",
            "error_message": error_msg,
            "result": {
                "deployed_url": result["deployed_url"],
                "deployment_result": result.get("deployment_result"),
                "smoke_result": smoke_result,
            },
        },
    )
    # Project stays active — deploy succeeded, service just unhealthy
    await api_client.patch(
        f"projects/{project_id}",
        json={"status": ProjectStatus.ACTIVE.value},
    )

    await publish_callback_event(
        redis,
        callback_stream,
        "failed",
        task_id,
        error_msg,
        user_id=user_id,
        project_id=project_id,
    )
    if not callback_stream:
        await publish_proactive_message(
            redis,
            user_id,
            f"Deployed {project_name} but smoke test failed: {smoke_details}",
        )

    return {
        "status": "failed",
        "error": error_msg,
        "deployed_url": result["deployed_url"],
        "finished_at": datetime.now(UTC).isoformat(),
    }


async def _handle_deploy_success(
    *,
    result: dict,
    smoke_result: dict | None,
    task_id: str,
    project_id: str,
    project: dict,
    callback_stream: str,
    user_id: str,
    redis: RedisStreamClient,
) -> dict:
    """Handle successful deploy (with or without smoke)."""
    logger.info(
        "deploy_job_success",
        task_id=task_id,
        deployed_url=result["deployed_url"],
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": "completed",
            "result": {
                "deployed_url": result["deployed_url"],
                "deployment_result": result.get("deployment_result"),
                "smoke_result": smoke_result,
            },
        },
    )
    await api_client.patch(
        f"projects/{project_id}",
        json={"status": ProjectStatus.ACTIVE.value},
    )

    await publish_callback_event(
        redis,
        callback_stream,
        "completed",
        task_id,
        f"Deploy completed: {result['deployed_url']}",
        user_id=user_id,
        project_id=project_id,
    )
    if not callback_stream:
        project_name = project.get("name", project_id) if project else project_id
        await publish_proactive_message(
            redis, user_id, f"Deployed {project_name}: {result['deployed_url']}"
        )

    return {
        "status": "success",
        "deployed_url": result["deployed_url"],
        "finished_at": datetime.now(UTC).isoformat(),
    }


def _build_subgraph_input(
    project_id: str, project: dict, git_url: str, allocated_resources: dict, job_data: dict
) -> dict:
    """Build DevOps subgraph input from deploy job data."""
    return {
        "project_id": project_id,
        "project_spec": project,
        "repo_info": {
            "full_name": git_url.replace("https://github.com/", "")
            .rstrip("/")
            .removesuffix(".git"),
            "html_url": git_url,
        },
        "allocated_resources": allocated_resources,
        "provided_secrets": job_data.get("provided_secrets", {}),
        "messages": [],
        "env_variables": [],
        "env_analysis": {},
        "resolved_secrets": {},
        "missing_user_secrets": [],
        "deployment_result": None,
        "deployed_url": None,
        "smoke_result": None,
        "errors": [],
    }


async def process_deploy_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single deploy job by running DevOps Subgraph.

    Args:
        job_data: Job data from Redis queue (task_id, project_id, user_id, callback_stream)
        redis: Redis client for publishing events

    Returns:
        Result dict with status and details
    """
    msg = DeployMessage.model_validate(job_data)
    task_id = msg.task_id
    project_id = msg.project_id
    callback_stream = msg.callback_stream
    user_id = msg.user_id

    logger.info(
        "deploy_job_started",
        task_id=task_id,
        project_id=project_id,
        triggered_by=msg.triggered_by.value,
    )

    try:
        # Deduplication guard: skip if another deploy is already running for this project
        cancel_result = await _check_duplicate_deploy(task_id, project_id)
        if cancel_result:
            return cancel_result

        # Update task status to running
        await api_client.patch(f"runs/{task_id}", json={"status": RunStatus.RUNNING.value})

        # Publish progress event
        await publish_callback_event(
            redis,
            callback_stream,
            "progress",
            task_id,
            "Deploy task started",
            user_id=user_id,
            project_id=project_id or "",
        )

        # Fetch project details (with user isolation)
        tg_kwargs = {"telegram_id": int(user_id)} if user_id and user_id.isdigit() else {}
        project: ProjectInfo | None = await api_client.get_project(project_id, **tg_kwargs)
        if not project:
            error_msg = f"Project {project_id} not found"
            await api_client.patch(
                f"runs/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )
            return {"status": "failed", "error": error_msg}

        # Get or create allocations for the project
        from ..tools.allocator import AllocationError, ensure_project_allocations

        try:
            # Get config from project
            config = project.get("config", {})
            modules = config.get("modules", ["backend"])
            min_ram_mb = config.get("estimated_ram_mb", 512)

            allocated_resources = await ensure_project_allocations(
                project_id=project_id,
                modules=modules,
                min_ram_mb=min_ram_mb,
            )
        except AllocationError as e:
            error_msg = str(e)
            await api_client.patch(
                f"runs/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )
            return {"status": "failed", "error": error_msg}

        # Update project status to deploying
        await api_client.patch(
            f"projects/{project_id}",
            json={"status": ProjectStatus.DEPLOYING.value},
        )

        # Resolve git_url from primary Repository entity
        primary_repo = await api_client.get_primary_repository(project_id)
        _git_url = primary_repo.get("git_url", "") if primary_repo else ""

        # Run DevOps subgraph
        devops_subgraph = create_devops_subgraph()
        subgraph_input = _build_subgraph_input(
            project_id, project, _git_url, allocated_resources, job_data
        )
        result = await devops_subgraph.ainvoke(subgraph_input)

        logger.info(
            "devops_subgraph_result",
            task_id=task_id,
            result_keys=sorted(result.keys()),
            has_smoke_result="smoke_result" in result,
            smoke_result=result.get("smoke_result"),
            deployed_url=result.get("deployed_url"),
            errors=result.get("errors"),
        )

        if result.get("deployed_url"):
            smoke_result = result.get("smoke_result")
            smoke_failed = smoke_result and smoke_result.get("status") == "fail"

            if smoke_failed:
                project_name = project.get("name", project_id) if project else project_id
                return await _handle_smoke_failure(
                    result=result,
                    smoke_result=smoke_result,
                    task_id=task_id,
                    project_id=project_id,
                    project_name=project_name,
                    callback_stream=callback_stream,
                    user_id=user_id,
                    redis=redis,
                )

            return await _handle_deploy_success(
                result=result,
                smoke_result=smoke_result,
                task_id=task_id,
                project_id=project_id,
                project=project,
                callback_stream=callback_stream,
                user_id=user_id,
                redis=redis,
            )
        elif result.get("missing_user_secrets"):
            missing = result.get("missing_user_secrets")
            logger.info("deploy_job_missing_secrets", task_id=task_id, missing=missing)
            error_msg = f"Missing secrets: {', '.join(missing)}"
            await api_client.patch(
                f"runs/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )
            # Roll back project status — don't leave it stuck in "deploying"
            await api_client.patch(
                f"projects/{project_id}",
                json={"status": ProjectStatus.FAILED.value},
            )

            await publish_callback_event(
                redis,
                callback_stream,
                "failed",
                task_id,
                error_msg,
                user_id=user_id,
                project_id=project_id or "",
            )
            if not callback_stream:
                project_name = project.get("name", project_id) if project else project_id
                await publish_proactive_message(
                    redis,
                    user_id,
                    f"Deploy blocked for {project_name} — missing: {', '.join(missing)}. "
                    "Please provide via bot.",
                )

            return {
                "status": "failed",
                "error": error_msg,
                "missing_secrets": missing,
                "finished_at": datetime.now(UTC).isoformat(),
            }
        else:
            errors = result.get("errors", ["Unknown deployment error"])
            logger.error("deploy_job_failed", task_id=task_id, errors=errors)
            error_msg = "; ".join(errors)
            await api_client.patch(
                f"runs/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )

            await publish_callback_event(
                redis,
                callback_stream,
                "failed",
                task_id,
                error_msg,
                user_id=user_id,
                project_id=project_id or "",
            )
            if not callback_stream:
                project_name = project.get("name", project_id) if project else project_id
                await publish_proactive_message(
                    redis, user_id, f"Deploy failed for {project_name}: {error_msg}"
                )

            return {
                "status": "failed",
                "error": error_msg,
                "finished_at": datetime.now(UTC).isoformat(),
            }

    except Exception as e:
        logger.error(
            "deploy_job_exception",
            task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        await api_client.patch(
            f"runs/{task_id}",
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
            f"Deploy task failed: {e!s}",
            user_id=user_id,
            project_id=project_id or "",
        )
        if not callback_stream:
            await publish_proactive_message(redis, user_id, f"Deploy failed: {e!s}")

        return {
            "status": "failed",
            "error": str(e),
            "finished_at": datetime.now(UTC).isoformat(),
        }


def main():
    """Entry point for running as module."""
    start_worker(
        service_name="deploy-worker",
        queue=DEPLOY_QUEUE,
        process_fn=process_deploy_job,
    )


if __name__ == "__main__":
    main()
