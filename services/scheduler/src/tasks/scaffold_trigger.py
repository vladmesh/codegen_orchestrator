"""Scaffold trigger — ensures workspace readiness before pipeline proceeds.

Runs as part of the task_dispatcher_loop cycle.

For DRAFT projects: publishes mode=full (copier + make setup + git push).
For ACTIVE projects with TODO tasks: publishes mode=ensure (clone + setup if missing).

Deduplication: uses Redis set ``scaffold:inflight`` to prevent duplicate
messages for the same project.  The scaffolder consumer must call
``clear_scaffold_inflight()`` after processing each job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.queues import SCAFFOLD_QUEUE

from ..startup import config as _config

if TYPE_CHECKING:
    from shared.redis_client import RedisStreamClient

    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)

# Redis key for tracking in-flight scaffold jobs (dedup)
SCAFFOLD_INFLIGHT_KEY = "scaffold:inflight"


def _scaffold_inflight_ttl() -> int:
    return _config.get_int("scheduler.scaffold_inflight_ttl") if _config else 600


def _template_config() -> tuple[str, str]:
    if _config is None:
        raise RuntimeError("Scheduler config is not initialized")
    return (
        _config.get("scheduler.service_template_source"),
        _config.get("scheduler.service_template_ref"),
    )


async def _mark_inflight(redis_client: RedisStreamClient, project_id: str) -> bool:
    """Mark project as having an in-flight scaffold job.

    Returns True if the mark was set (project was NOT already inflight).
    Returns False if the project already has an inflight job (duplicate).
    """
    member_key = f"{SCAFFOLD_INFLIGHT_KEY}:{project_id}"
    was_set = await redis_client.redis.set(member_key, "1", nx=True, ex=_scaffold_inflight_ttl())
    return bool(was_set)


async def clear_scaffold_inflight(redis_client: RedisStreamClient, project_id: str) -> None:
    """Remove the inflight marker for a project (called by scaffolder after processing)."""
    member_key = f"{SCAFFOLD_INFLIGHT_KEY}:{project_id}"
    await redis_client.redis.delete(member_key)


async def trigger_scaffolds(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> int:
    """Ensure workspace readiness for projects that need it.

    Publishes to scaffold:queue in two modes:
    - DRAFT projects with stories → mode=full (first-time scaffold)
    - ACTIVE projects with TODO tasks and workspace_ready=false → mode=ensure

    Returns:
        Number of scaffold jobs published.
    """
    projects = await api_client.get_projects()

    triggered = 0
    for project in projects:
        project_id = str(project.id)
        log = logger.bind(project_id=project_id, project_name=project.name)

        if project.status == ProjectStatus.DRAFT:
            if await _trigger_full_scaffold(project, api_client, redis_client, log):
                triggered += 1
        elif project.status == ProjectStatus.ACTIVE:
            if await _trigger_ensure_scaffold(
                project,
                api_client,
                redis_client,
                log,
            ):
                triggered += 1

    return triggered


async def _trigger_full_scaffold(project, api_client, redis_client, log) -> bool:
    """Trigger full scaffold for DRAFT projects."""
    project_id = str(project.id)
    config = project.config or {}

    if config.get("scaffold_error"):
        return False

    stories = await api_client.get_stories_by_project(project_id)
    if not stories:
        return False

    repos = await api_client.get_repositories(project_id)
    if not repos:
        log.debug("scaffold_skip_no_repo")
        return False

    if not await _mark_inflight(redis_client, project_id):
        log.debug("scaffold_skip_already_inflight", mode="full")
        return False

    repo = repos[0]
    msg = _build_scaffold_message(project, repo.id, mode="full")
    await redis_client.publish_message(SCAFFOLD_QUEUE, msg)
    log.info("scaffold_triggered", repository_id=repo.id, mode="full")
    return True


async def _trigger_ensure_scaffold(project, api_client, redis_client, log) -> bool:
    """Trigger ensure-workspace for ACTIVE projects with pending tasks."""
    project_id = str(project.id)
    config = project.config or {}

    # Skip if workspace is already marked ready or scaffold previously failed
    if config.get("workspace_ready"):
        return False
    if config.get("scaffold_error"):
        return False

    # Check for TODO tasks (pending dispatch)
    todo_tasks = await api_client.get_tasks_by_project_and_status(
        project_id,
        TaskStatus.TODO,
    )
    if not todo_tasks:
        return False

    repos = await api_client.get_repositories(project_id)
    if not repos:
        return False

    if not await _mark_inflight(redis_client, project_id):
        log.debug("scaffold_skip_already_inflight", mode="ensure")
        return False

    repo = repos[0]
    msg = _build_scaffold_message(project, repo.id, mode="ensure")
    await redis_client.publish_message(SCAFFOLD_QUEUE, msg)
    log.info("scaffold_triggered", repository_id=repo.id, mode="ensure")
    return True


def _build_scaffold_message(project, repo_id: str, mode: str) -> ScaffoldMessage:
    """Build a ScaffoldMessage from project data."""
    config = project.config or {}
    config_modules = config.get("modules", ["backend"])
    modules = ",".join(config_modules) if config_modules else "backend"
    template_repo, template_ref = _template_config()
    return ScaffoldMessage(
        project_id=str(project.id),
        repository_id=repo_id,
        user_id=str(project.owner_id),
        template_repo=template_repo,
        template_ref=template_ref,
        project_name=project.name,
        modules=modules,
        task_description=config.get("description", project.description or ""),
        mode=mode,
    )
