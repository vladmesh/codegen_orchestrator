"""Read the planning Task and repository an engineering run is judged and built by.

Every producer of an engineering message ends up here, so this is the one place
the worker's TASK.md learns what QA checks and what package source, if any, the
task must produce. The named planning Task is authoritative and its read must
succeed; the repository checklist central QA runs after deploy is best-effort.
"""

from __future__ import annotations

import structlog

from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.task import TaskDTO, TaskType
from shared.contracts.queues.engineering import EngineeringMessage

from ..clients.api import LanggraphAPIClient

logger = structlog.get_logger(__name__)


class PlanningTaskRefused(ValueError):
    """The named planning Task or this attempt's Run is not the message's own work."""


async def load_planning_task(
    api_client: LanggraphAPIClient, msg: EngineeringMessage
) -> TaskDTO | None:
    """The message's planning Task as the API stores it, or None for taskless work.

    A failed or malformed read propagates. A Task or Run naming another task, product
    or story raises `PlanningTaskRefused`, so no intent is acted on for foreign work.
    """
    if msg.planning_task_id is None:
        return None
    task = await api_client.get_task(msg.planning_task_id)
    run = await api_client.get_run(msg.task_id)
    identity = (msg.planning_task_id, msg.project_id, msg.story_id)
    if (task.id, str(task.project_id), task.story_id) != identity:
        raise PlanningTaskRefused(f"task {task.id} is not this message's task")
    if (run.task_id, str(run.project_id), run.story_id) != identity:
        raise PlanningTaskRefused(f"run {msg.task_id} is not this message's attempt")
    return task


def package_build_refusal(
    task: TaskDTO | None, repository: RepositoryDTO | None, project_id: str
) -> str | None:
    """Why a package-build Task may not reach a worker on `repository`, or None."""
    if task is None or task.package_build is None:
        return None
    if task.type is not TaskType.FEATURE:
        return f"package build task {task.id} is not a feature"
    if (
        repository is None
        or repository.id != task.repository_id
        or str(repository.project_id) != project_id
        or not repository.is_managed
    ):
        return f"package build task {task.id} does not own the product's managed repository"
    return None


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
