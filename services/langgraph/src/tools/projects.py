"""Project management tools for agents."""

import uuid
from typing import Annotated, Any

from langchain_core.tools import tool

from .base import api_client


@tool
async def create_project(
    name: Annotated[str, "Project name in snake_case (e.g., 'weather_bot')"],
    description: Annotated[str, "Brief project description"],
    modules: Annotated[list[str], "Modules to generate: backend, tg_bot, notifications, frontend"],
    entry_points: Annotated[list[str], "Entry points: telegram, frontend, api"],
    telegram_token: Annotated[str | None, "Telegram Bot Token (if applicable)"] = None,
) -> dict[str, Any]:
    """Create a new project in the database.

    Call this when you have gathered enough information about the project.
    Returns the created project with its ID.

    After creation, the project will be passed to Zavhoz for resource allocation.
    """
    project_id = str(uuid.uuid4())[:8]

    config_payload = {
        "description": description,
        "modules": modules,
        "entry_points": entry_points,
        "estimated_ram_mb": 512,
        "estimated_disk_mb": 2048,
    }

    if telegram_token:
        config_payload["secrets"] = {"telegram_token": telegram_token}

    payload = {
        "id": project_id,
        "name": name,
        "status": "pending_resources",
        "config": config_payload,
    }

    return await api_client.post("/projects/", json=payload)


@tool
async def list_projects(
    status: Annotated[str | None, "Optional project status filter"] = None,
) -> list[dict[str, Any]]:
    """List projects from the database.

    Args:
        status: Optional project status to filter by.

    Returns:
        List of project records.
    """
    params = {"status": status} if status else None
    return await api_client.get("/projects/", params=params)


@tool
async def get_project_status(
    project_id: Annotated[str, "Project ID"],
) -> dict[str, Any]:
    """Get a single project's status and metadata."""
    return await api_client.get(f"/projects/{project_id}")


@tool
async def create_project_intent(
    intent: Annotated[str, "Intent type: new_project | update_project"],
    summary: Annotated[str, "Short summary of the user's request"],
    project_id: Annotated[str | None, "Project ID if applicable"] = None,
) -> dict[str, Any]:
    """Create a project intent for the orchestrator flow.

    This does not persist anything to the database; it only returns
    structured intent metadata for the Product Owner node.
    """
    return {"intent": intent, "summary": summary, "project_id": project_id}


@tool
async def set_project_maintenance(
    project_id: Annotated[str, "Project ID to update"],
    update_description: Annotated[str, "Description of the update/feature to implement"],
) -> dict[str, Any]:
    """Set a project to maintenance status for updates.

    Use this when the user wants to update or add features to an existing project.
    This will trigger the Engineering flow (Architect → Developer → Tester).

    Args:
        project_id: ID of the project to update
        update_description: Description of what needs to be updated

    Returns:
        Updated project details
    """
    # First verify project exists
    resp = await api_client.get_raw(f"/projects/{project_id}")
    if resp.status_code == 404:
        return {"error": f"Project {project_id} not found"}
    resp.raise_for_status()
    project = resp.json()

    # Update status to maintenance
    return await api_client.patch(
        f"/projects/{project_id}",
        json={
            "status": "maintenance",
            "config": {
                **project.get("config", {}),
                "maintenance_request": update_description,
            },
        },
    )
