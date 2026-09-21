"""Read the acceptance criteria an engineering run is judged by.

Every producer of an engineering message ends up here, so this is the one place
the worker's TASK.md learns what QA checks: the planning task's own criteria and
the project's cumulative repository checklist that central QA runs after deploy.
Neither read may fail the run — a missing criteria section is worse for the
worker than a failed attempt is for the pipeline, but not worth one.
"""

from __future__ import annotations

import structlog

from shared.contracts.dto.repository import RepositoryDTO

from ..clients.api import LanggraphAPIClient

logger = structlog.get_logger(__name__)


async def load_task_acceptance_criteria(
    api_client: LanggraphAPIClient, planning_task_id: str | None
) -> str | None:
    """The planning task's criteria, or None when there is no task or it is unreadable."""
    if not planning_task_id:
        return None
    try:
        task = await api_client.get_task(planning_task_id)
    except Exception:
        logger.warning(
            "task_acceptance_criteria_unreadable",
            planning_task_id=planning_task_id,
            exc_info=True,
        )
        return None
    return task.acceptance_criteria


async def load_primary_repository(
    api_client: LanggraphAPIClient, project_id: str
) -> RepositoryDTO | None:
    """The project's primary repository, or None when it is missing or unreadable."""
    try:
        repository = await api_client.get_primary_repository(project_id)
    except Exception:
        logger.warning(
            "repository_acceptance_criteria_unreadable", project_id=project_id, exc_info=True
        )
        return None
    if repository is None:
        logger.warning("repository_acceptance_criteria_missing_repository", project_id=project_id)
    return repository
