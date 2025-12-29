"""Project management tools for agents."""

from http import HTTPStatus
import re
from typing import Annotated
import uuid

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from ..schemas.tools import ProjectCreateResult, ProjectInfo, ProjectIntent
from .base import api_client

# Regex for valid project names: lowercase, starts with letter, only letters/numbers/hyphens
PROJECT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


def validate_project_name(name: str) -> None:
    """Validate project name format.

    Project name must be:
    - Lowercase only
    - Start with a letter
    - Contain only letters, numbers, and hyphens

    Raises:
        ValueError: If name doesn't match the required format.
    """
    if not name:
        raise ValueError("Project name cannot be empty")

    if not PROJECT_NAME_PATTERN.match(name):
        if name[0].isdigit() or name[0] == "-":
            raise ValueError(
                f"Invalid project name '{name}': must start with a letter. "
                "Use lowercase letters, numbers, and hyphens only (e.g., 'my-project')."
            )
        if name != name.lower():
            raise ValueError(
                f"Invalid project name '{name}': must be lowercase. "
                "Use lowercase letters, numbers, and hyphens only (e.g., 'my-project')."
            )
        raise ValueError(
            f"Invalid project name '{name}': must contain only letters, numbers, and hyphens. "
            "Example: 'my-cool-project'."
        )


@tool
async def create_project(
    name: Annotated[
        str,
        "Project name: lowercase, starts with letter, only a-z/0-9/hyphens",
    ],
    description: Annotated[str, "Brief project description"],
    modules: Annotated[list[str], "Modules to generate: backend, tg_bot, notifications, frontend"],
    entry_points: Annotated[list[str], "Entry points: telegram, frontend, api"],
    telegram_token: Annotated[str | None, "Telegram Bot Token (if applicable)"] = None,
    detailed_spec: Annotated[str | None, "Full detailed project specification in Markdown"] = None,
    # Injected from graph state - not visible to LLM
    state: Annotated[dict, InjectedState] = None,
) -> ProjectCreateResult:
    """Create a new project in the database.

    Call this when you have gathered enough information about the project.
    Returns the created project with its ID.

    After creation, the project will be passed to Zavhoz for resource allocation.
    """
    # Validate project name format before proceeding
    validate_project_name(name)

    project_id = str(uuid.uuid4())[:8]

    config_payload = {
        "name": name,  # Include name for downstream nodes (architect, etc.)
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

    # Build headers with user context for ownership assignment
    headers = {}
    if state and state.get("telegram_user_id"):
        headers["X-Telegram-ID"] = str(state["telegram_user_id"])

    resp = await api_client.post("/projects/", json=payload, headers=headers or None)
    return ProjectCreateResult(**resp)


@tool
async def list_projects(
    status: Annotated[str | None, "Optional project status filter"] = None,
    # Injected from graph state - not visible to LLM
    state: Annotated[dict, InjectedState] = None,
) -> list[ProjectInfo]:
    """List projects from the database.

    Args:
        status: Optional project status to filter by.

    Returns:
        List of project records.
    """
    params = {"status": status} if status else None

    # Build headers with user context for ownership filtering
    headers = {}
    if state and state.get("telegram_user_id"):
        headers["X-Telegram-ID"] = str(state["telegram_user_id"])

    resp = await api_client.get("/projects/", params=params, headers=headers or None)
    return [ProjectInfo(**p) for p in resp]


@tool
async def get_project_status(
    project_id: Annotated[str, "Project ID"],
    # Injected from graph state - not visible to LLM
    state: Annotated[dict, InjectedState] = None,
) -> ProjectInfo:
    """Get a single project's status and metadata."""
    # Build headers with user context for ownership check
    headers = {}
    if state and state.get("telegram_user_id"):
        headers["X-Telegram-ID"] = str(state["telegram_user_id"])

    resp = await api_client.get(f"/projects/{project_id}", headers=headers or None)
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
    # Injected from graph state - not visible to LLM
    state: Annotated[dict, InjectedState] = None,
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
    # Build headers with user context for ownership check
    headers = {}
    if state and state.get("telegram_user_id"):
        headers["X-Telegram-ID"] = str(state["telegram_user_id"])

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
        headers=headers or None,
    )
    return ProjectInfo(**updated)
