"""Engineering Worker — consumes from jobs:engineering queue.

Run standalone: python -m src.consumers.engineering
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.run import RunStatus
from shared.queues import ENGINEERING_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..clients.story_worker_registry import get_story_worker
from ..nodes.resource_allocator import resource_allocator_node
from ..tracing import build_langfuse_metadata, get_langfuse_callbacks
from ._base import start_worker
from ._events import publish_callback_event
from ._repo_setup import _create_repo_and_set_secrets
from .engineering_result_handler import (
    _update_task_status,
    _write_task_event,
    fail_job as _fail_job,
    handle_engineering_success as _handle_engineering_success,
    handle_worker_gave_up as _handle_worker_gave_up,
)
from .story_context import (
    build_story_context as _build_story_context,
    build_story_md as _build_story_md,
)

# Re-export for backward compatibility with tests
__all__ = [
    "_build_story_context",
    "_build_story_md",
    "_fail_job",
    "_handle_engineering_success",
    "_handle_worker_gave_up",
    "_update_task_status",
    "_write_task_event",
    "process_engineering_job",
]


def _parse_telegram_id(user_id: str) -> dict:
    """Build get_project kwargs with telegram_id if user_id is numeric."""
    if user_id and user_id.isdigit():
        return {"telegram_id": int(user_id)}
    return {}


logger = structlog.get_logger(__name__)


async def _resolve_allocations(task_id: str, project_id: str, project: ProjectDTO) -> dict | None:
    """Resolve or create resource allocations. Returns dict or None on failure."""
    logger.info("allocating_resources", task_id=task_id, project_id=project_id)
    result = await resource_allocator_node.run(
        {
            "project_id": project_id,
            "project_spec": project.model_dump(),
            "allocated_resources": {},
            "errors": [],
        }
    )
    if result.get("errors"):
        error_msg = "; ".join(result["errors"])
        logger.error("resource_allocation_failed", task_id=task_id, errors=result["errors"])
        await api_client.patch(
            f"runs/{task_id}", json={"status": "failed", "error_message": error_msg}
        )
        return None

    allocated = result.get("allocated_resources", {})
    logger.info("resources_allocated", task_id=task_id, count=len(allocated))
    return allocated


async def process_engineering_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single engineering job by running Engineering Subgraph."""
    from ..subgraphs.engineering import create_engineering_subgraph

    task_id = job_data.get("task_id", "unknown")
    project_id = job_data.get("project_id")
    callback_stream = job_data.get("callback_stream")
    action = job_data.get("action", "create")
    description = job_data.get("description")
    skip_deploy = job_data.get("skip_deploy", False)
    user_id = job_data.get("user_id", "")
    planning_task_id = job_data.get("planning_task_id")
    story_id = job_data.get("story_id")
    deploy_fix_attempt = job_data.get("deploy_fix_attempt", 0)

    logger.info(
        "engineering_job_started",
        task_id=task_id,
        project_id=project_id,
        action=action,
    )

    try:
        await api_client.patch(
            f"runs/{task_id}",
            json={"status": RunStatus.RUNNING.value, "started_at": datetime.now(UTC).isoformat()},
        )

        await publish_callback_event(
            redis,
            callback_stream,
            "progress",
            task_id,
            "Engineering task started",
            user_id=user_id,
            project_id=project_id or "",
        )

        project = await api_client.get_project(project_id, **_parse_telegram_id(user_id))
        if not project:
            return await _fail_job(task_id, f"Project {project_id} not found", planning_task_id)

        if not description:
            description = (project.config or {}).get("description", "")

        project_status = project.status
        if project_status == ProjectStatus.DRAFT and action == "create":
            await _create_repo_and_set_secrets(project)
        elif project_status == ProjectStatus.DRAFT and action != "create":
            logger.warning(
                "feature_fix_on_draft_project",
                task_id=task_id,
                project_id=project_id,
                action=action,
                hint="Project is in draft status but action is not 'create'. "
                "Skipping scaffolding — developer will work with existing repo.",
            )

        allocated_resources = await _resolve_allocations(task_id, project_id, project)
        if allocated_resources is None:
            return {"status": "failed", "error": "Resource allocation failed"}

        existing_worker_id = None
        if story_id:
            existing_worker_id = await get_story_worker(redis.redis, story_id)
            if existing_worker_id:
                logger.info(
                    "reusing_story_worker",
                    story_id=story_id,
                    worker_id=existing_worker_id,
                    task_id=task_id,
                )

        primary_repo = await api_client.get_primary_repository(project_id)
        repo_id = primary_repo.id if primary_repo else None

        story_context = await _build_story_context(story_id, planning_task_id) if story_id else None
        story_md = await _build_story_md(story_id, planning_task_id) if story_id else None

        branch = f"story/{story_id}" if story_id else None

        subgraph_input = {
            "messages": [],
            "current_project": project_id,
            "project_spec": project.model_dump(),
            "allocated_resources": allocated_resources,
            "action": action,
            "description": description,
            "story_context": story_context,
            "story_md": story_md,
            "repo_id": repo_id,
            "commit_sha": None,
            "worker_id": existing_worker_id,
            "engineering_status": EngineeringStatus.IDLE,
            "iteration_count": 0,
            "test_results": None,
            "needs_human_approval": False,
            "human_approval_reason": None,
            "branch": branch,
            "worker_report": None,
            "reject_reason": None,
            "errors": [],
        }

        engineering_subgraph = create_engineering_subgraph()
        developer_started_at = datetime.now(UTC)
        result = await engineering_subgraph.ainvoke(
            subgraph_input,
            config={
                "callbacks": get_langfuse_callbacks(),
                "metadata": build_langfuse_metadata(
                    agent_type="engineering",
                    user_id=user_id,
                    project_id=project_id,
                    task_id=task_id,
                    story_id=story_id,
                ),
            },
        )

        worker_report = result.get("worker_report")
        if worker_report and planning_task_id:
            await _write_task_event(
                api_client,
                planning_task_id,
                "worker_report",
                {"report": worker_report},
            )
            logger.info(
                "worker_report_saved",
                task_id=task_id,
                planning_task_id=planning_task_id,
                report_size=len(worker_report),
            )

        eng_status = result.get("engineering_status", EngineeringStatus.FAILED)

        if eng_status == EngineeringStatus.DONE:
            logger.info(
                "engineering_job_success",
                task_id=task_id,
                commit_sha=result.get("commit_sha"),
            )
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
                story_id=story_id,
                deploy_fix_attempt=deploy_fix_attempt,
            )

        elif eng_status == EngineeringStatus.GAVE_UP:
            reason = (
                result.get("block_reason")
                or result.get("reject_reason")
                or "Worker could not complete the task"
            )
            return await _handle_worker_gave_up(
                task_id=task_id,
                project_id=project_id,
                planning_task_id=planning_task_id,
                story_id=story_id,
                reason=reason,
                user_id=user_id,
                redis=redis,
            )
        else:
            # FAILED (technical) or unexpected status — treat as technical failure
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


def main():
    """Entry point for running as module."""
    start_worker(
        service_name="engineering-worker",
        queue=ENGINEERING_QUEUE,
        process_fn=process_engineering_job,
    )


if __name__ == "__main__":
    main()
