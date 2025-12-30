"""Infrastructure capability tools for Dynamic ProductOwner.

Provides tools to manage port allocations and resources.
Phase 4.2 addition.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import tool
import structlog

from ..state.context import get_current_state
from .base import api_client

logger = structlog.get_logger(__name__)


@tool
async def list_allocations(
    project_id: Annotated[str | None, "Filter by project ID (optional)"] = None,
) -> dict:
    """List allocated resources (ports/servers).

    Args:
        project_id: Filter by specific project (optional, shows all if not specified)

    Returns:
        {
            "allocations": [
                {
                    "id": 123,
                    "project_id": "hello-world-bot",
                    "server_handle": "vps-267179",
                    "port": 8080,
                    "service_name": "hello-world-bot",
                    "allocated_at": "2024-01-15T10:00:00Z"
                },
                ...
            ]
        }
    """
    # Get current state for logging/audit purposes
    _ = get_current_state()

    if project_id:
        allocations = await api_client.get_project_allocations(project_id)
    else:
        # Get all allocations for the user's projects
        # For now, we list all since we're internal
        allocations = await api_client.get(
            "allocations/",
            params={},
        )

    logger.debug(
        "allocations_listed",
        project_id=project_id,
        count=len(allocations) if isinstance(allocations, list) else 0,
    )

    return {"allocations": allocations if isinstance(allocations, list) else []}


@tool
async def release_port(
    allocation_id: Annotated[int, "Allocation ID from list_allocations"],
    confirm: Annotated[bool, "Must be True to proceed with release"] = False,
) -> dict:
    """Release allocated port and free resources.

    WARNING: This will make the deployed service inaccessible!
    The service will need to be redeployed to a new port.

    Args:
        allocation_id: Allocation ID from list_allocations
        confirm: Must be True to proceed

    Returns:
        {"released": True} or {"error": "..."}
    """
    if not confirm:
        return {
            "error": "Set confirm=True to release. This will stop the service!",
            "allocation_id": allocation_id,
            "hint": "Use list_allocations first to verify you have the correct allocation.",
        }

    state = get_current_state()
    user_id = state.get("telegram_user_id")

    # Get allocation details first
    allocation = await api_client.get_allocation(allocation_id)
    if not allocation:
        return {"error": f"Allocation {allocation_id} not found"}

    # Check ownership via project's user_id
    project_id = allocation.get("project_id")
    if project_id:
        project = await api_client.get_project(project_id)
        if project:
            project_user_id = project.get("user_id")
            if project_user_id and str(project_user_id) != str(user_id):
                logger.warning(
                    "release_port_unauthorized",
                    allocation_id=allocation_id,
                    project_user_id=project_user_id,
                    requesting_user_id=user_id,
                )
                return {"error": "You don't have permission to release this allocation"}

    # Release the allocation
    success = await api_client.release_allocation(allocation_id)

    if success:
        logger.info(
            "port_released",
            allocation_id=allocation_id,
            project_id=allocation.get("project_id"),
            server_handle=allocation.get("server_handle"),
            port=allocation.get("port"),
            released_by=user_id,
        )
        return {
            "released": True,
            "allocation_id": allocation_id,
            "project_id": allocation.get("project_id"),
            "server_handle": allocation.get("server_handle"),
            "port": allocation.get("port"),
        }
    else:
        return {
            "error": f"Failed to release allocation {allocation_id}",
            "allocation_id": allocation_id,
        }
