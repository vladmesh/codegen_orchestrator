"""Project management tools for agents."""

from http import HTTPStatus
from typing import Annotated
import uuid

from langchain_core.tools import tool

from ..schemas.tools import ProjectCreateResult, ProjectInfo, ProjectIntent
from .base import api_client


@tool
async def create_project(
    name: Annotated[str, "Project name in snake_case (e.g., 'weather_bot')"],
    description: Annotated[str, "Brief project description"],
    modules: Annotated[list[str], "Modules to generate: backend, tg_bot, notifications, frontend"],
    entry_points: Annotated[list[str], "Entry points: telegram, frontend, api"],
    telegram_token: Annotated[str | None, "Telegram Bot Token (if applicable)"] = None,
    detailed_spec: Annotated[str | None, "Full detailed project specification in Markdown"] = None,
) -> ProjectCreateResult:
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

    if detailed_spec:
        config_payload["detailed_spec"] = detailed_spec

    if telegram_token:
        config_payload["secrets"] = {"telegram_token": telegram_token}

    payload = {
        "id": project_id,
        "name": name,
        "status": "pending_resources",
        "config": config_payload,
    }

    resp = await api_client.post("/projects/", json=payload)
    return ProjectCreateResult(**resp)


@tool
async def list_projects(
    status: Annotated[str | None, "Optional project status filter"] = None,
) -> list[ProjectInfo]:
    """List projects from the database.

    Args:
        status: Optional project status to filter by.

    Returns:
        List of project records.
    """
    params = {"status": status} if status else None
    resp = await api_client.get("/projects/", params=params)
    return [ProjectInfo(**p) for p in resp]


@tool
async def get_project_status(
    project_id: Annotated[str, "Project ID"],
) -> ProjectInfo:
    """Get a single project's status and metadata."""
    resp = await api_client.get(f"/projects/{project_id}")
    return ProjectInfo(**resp)


@tool
async def create_project_intent(
    intent: Annotated[str, "Intent type: new_project | update_project"],
    summary: Annotated[str, "Short summary of the user's request"],
    project_id: Annotated[str | None, "Project ID if applicable"] = None,
) -> ProjectIntent:
    """Create a project intent for the orchestrator flow.

    This does not persist anything to the database; it only returns
    structured intent metadata for the Product Owner node.
    """
    return ProjectIntent(intent=intent, summary=summary, project_id=project_id)


@tool
async def set_project_maintenance(
    project_id: Annotated[str, "Project ID to update"],
    update_description: Annotated[str, "Description of the update/feature to implement"],
) -> ProjectInfo:
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
    if resp.status_code == HTTPStatus.NOT_FOUND:
        raise ValueError(f"Project {project_id} not found")
    resp.raise_for_status()
    project = resp.json()

    # Update status to maintenance
    updated = await api_client.patch(
        f"/projects/{project_id}",
        json={
            "status": "maintenance",
            "config": {
                **project.get("config", {}),
                "maintenance_request": update_description,
            },
        },
    )
    return ProjectInfo(**updated)
