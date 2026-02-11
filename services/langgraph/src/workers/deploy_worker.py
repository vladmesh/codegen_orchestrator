"""Deploy Worker — consumes from jobs:deploy queue and runs DevOps.

Run standalone: python -m src.workers.deploy_worker
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.queues import DEPLOY_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..schemas.api_types import ProjectInfo
from ..subgraphs.devops import create_devops_subgraph
from ._base import start_worker
from ._events import publish_callback_event

logger = structlog.get_logger(__name__)


async def process_deploy_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single deploy job by running DevOps Subgraph.

    Args:
        job_data: Job data from Redis queue (task_id, project_id, user_id, callback_stream)
        redis: Redis client for publishing events

    Returns:
        Result dict with status and details
    """
    task_id = job_data.get("task_id", "unknown")
    project_id = job_data.get("project_id")
    callback_stream = job_data.get("callback_stream")

    logger.info("deploy_job_started", task_id=task_id, project_id=project_id)

    try:
        # Update task status to running
        await api_client.patch(f"tasks/{task_id}", json={"status": "running"})

        # Publish progress event
        await publish_callback_event(
            redis, callback_stream, "progress", task_id, "Deploy task started"
        )

        # Fetch project details
        project: ProjectInfo | None = await api_client.get_project(project_id)
        if not project:
            error_msg = f"Project {project_id} not found"
            await api_client.patch(
                f"tasks/{task_id}",
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
                f"tasks/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )
            return {"status": "failed", "error": error_msg}

        # Update project status to deploying
        await api_client.patch(
            f"projects/{project_id}",
            json={"status": ProjectStatus.DEPLOYING.value},
        )

        # Run DevOps subgraph
        devops_subgraph = create_devops_subgraph()

        # Prepare subgraph input with enriched server_ip
        subgraph_input = {
            "project_id": project_id,
            "project_spec": project,
            "repo_info": {
                "full_name": project.get("repository_url", "")
                .replace("https://github.com/", "")
                .rstrip(".git"),
                "html_url": project.get("repository_url"),
            },
            "allocated_resources": allocated_resources,
            "provided_secrets": job_data.get("provided_secrets", {}),
            # Initialize empty fields
            "messages": [],
            "env_variables": [],
            "env_analysis": {},
            "resolved_secrets": {},
            "missing_user_secrets": [],
            "deployment_result": None,
            "deployed_url": None,
            "errors": [],
        }

        result = await devops_subgraph.ainvoke(subgraph_input)

        if result.get("deployed_url"):
            logger.info(
                "deploy_job_success",
                task_id=task_id,
                deployed_url=result["deployed_url"],
            )
            await api_client.patch(
                f"tasks/{task_id}",
                json={
                    "status": "completed",
                    "result": {
                        "deployed_url": result["deployed_url"],
                        "deployment_result": result.get("deployment_result"),
                    },
                },
            )
            # Update project status to active
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
            )

            return {
                "status": "success",
                "deployed_url": result["deployed_url"],
                "finished_at": datetime.now(UTC).isoformat(),
            }
        elif result.get("missing_user_secrets"):
            missing = result.get("missing_user_secrets")
            logger.info("deploy_job_missing_secrets", task_id=task_id, missing=missing)
            error_msg = f"Missing secrets: {', '.join(missing)}"
            await api_client.patch(
                f"tasks/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )

            await publish_callback_event(redis, callback_stream, "failed", task_id, error_msg)

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
                f"tasks/{task_id}",
                json={"status": "failed", "error_message": error_msg},
            )

            await publish_callback_event(redis, callback_stream, "failed", task_id, error_msg)

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
            redis, callback_stream, "failed", task_id, f"Deploy task failed: {e!s}"
        )

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
