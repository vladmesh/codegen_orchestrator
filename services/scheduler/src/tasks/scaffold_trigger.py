"""Scaffold trigger — detects draft projects with stories and publishes to scaffold:queue.

Runs as part of the task_dispatcher_loop cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.queues import SCAFFOLD_QUEUE

if TYPE_CHECKING:
    from shared.redis_client import RedisStreamClient

    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)

# Default template for new projects
DEFAULT_TEMPLATE_REPO = "gh:project-factory-organization/service-template"


async def trigger_scaffolds(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> int:
    """Find draft projects with stories and publish scaffold jobs.

    Only publishes if:
    - project.status == draft (not scaffolding/scaffolded/etc.)
    - project has at least one story
    - project has at least one repository

    Returns:
        Number of scaffold jobs published.
    """
    projects = await api_client.get_projects()

    triggered = 0
    for project in projects:
        if project.status != ProjectStatus.DRAFT:
            continue

        project_id = str(project.id)
        log = logger.bind(project_id=project_id, project_name=project.name)

        # Check for stories
        stories = await api_client.get_stories_by_project(project_id)
        if not stories:
            continue

        # Check for repository
        repos = await api_client.get_repositories(project_id)
        if not repos:
            log.debug("scaffold_skip_no_repo")
            continue

        repo = repos[0]  # Primary repo (1:1 for now)
        repo_id = repo["id"]

        # Build scaffold message — modules live in config, not as a top-level field
        config = project.config or {}
        config_modules = config.get("modules", ["backend"])
        modules = ",".join(config_modules) if config_modules else "backend"
        msg = ScaffoldMessage(
            project_id=project_id,
            repository_id=repo_id,
            user_id=str(project.owner_id),
            template_repo=DEFAULT_TEMPLATE_REPO,
            project_name=project.name,
            modules=modules,
            task_description=config.get("description", project.description or ""),
        )

        await redis_client.publish_message(SCAFFOLD_QUEUE, msg)

        log.info("scaffold_triggered", repository_id=repo_id, modules=modules)
        triggered += 1

    return triggered
