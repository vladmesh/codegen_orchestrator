"""Shared supervisor configuration, failure reporting, and admission checks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError
import structlog

from shared.contracts.dto.application import ApplicationStatus
from shared.notifications import notify_admins_best_effort
from shared.server_admission import (
    provisioning_failed_server_handles,
    server_admits_application,
)

if TYPE_CHECKING:
    from ...clients.api import SchedulerAPIClient

from ... import startup

logger = structlog.get_logger(__name__)

STORY_HUMAN_REVIEW_ACTION = "human-review"


def _qa_handoff_recovery_minutes() -> int:
    return startup.get_config().get_int("supervisor.qa_handoff_recovery_minutes")


def _resource_wait_timeout_minutes() -> int:
    return startup.get_config().get_int("supervisor.resource_wait_timeout_minutes")


def _resource_wait_metrics_freshness_seconds() -> int:
    return startup.get_config().get_int("supervisor.resource_wait_metrics_freshness_seconds")


def _parse_datetime(value: str | datetime) -> datetime:
    """Parse ISO datetime string or pass through datetime objects.

    Handles both Z and +00:00 suffixes for string inputs.
    """
    if isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


async def _admissible_target_exists(
    api_client: SchedulerAPIClient, *, required_ram_mb: int, min_disk_mb: int
) -> bool:
    """Whether any server could take this request right now.

    Both waits ask this: the engineering task parked in `waiting_resources` and
    the deploy run parked on `WAITING_INFRASTRUCTURE`. Admissibility itself comes
    from `shared.server_admission`, the predicate the allocator applies, so no
    wait can end towards a target the allocator would refuse.
    """
    now = datetime.now(UTC)
    provisioning_failed_handles = provisioning_failed_server_handles(
        await api_client.list_active_incidents()
    )
    for server in await api_client.get_servers():
        if not server_admits_application(server, provisioning_failed_handles):
            continue
        if server.capacity_ram_mb < required_ram_mb or server.capacity_disk_mb < min_disk_mb:
            continue
        if not server.last_health_check:
            continue
        checked = server.last_health_check
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        age = (now - checked).total_seconds()
        if not 0 <= age <= _resource_wait_metrics_freshness_seconds():
            continue
        apps = await api_client.get_applications(server.handle)
        reserved = sum(
            app.reserved_ram_mb
            for app in apps
            if app.status not in {ApplicationStatus.NOT_DEPLOYED, ApplicationStatus.STOPPED}
        )
        if server.capacity_ram_mb >= max(reserved, server.used_ram_mb) + required_ram_mb:
            return True
    return False


async def _fail_story_on_invalid_result(
    api_client: SchedulerAPIClient,
    story_id: str,
    project_id: str,
    run_type: str,
    exc: ValidationError,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Route a story whose latest run has an unparseable result to a terminal, visible state.

    A legacy or corrupt `run.result` would otherwise fail validation on every poll and
    wedge the story forever. Fail it once, loudly, and notify admins — no silent skip,
    no infinite retry.
    """
    log.error("run_result_invalid", run_type=run_type, error=str(exc))
    await api_client.fail_story(story_id)
    await _notify_admin_failure(story_id, project_id, f"invalid {run_type} run result: {exc}")


async def _notify_admin_failure(entity_id: str, project_id: str, error: str) -> None:
    """Notify after a terminal failure has already been committed."""
    await notify_admins_best_effort(
        f"Supervisor failure for {entity_id} (project {project_id}):\n{error[:500]}",
        level="error",
        component="supervisor",
        run_id=entity_id,
        project_id=project_id,
    )
