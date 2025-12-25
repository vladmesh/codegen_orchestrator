"""Resource inventory and deployment tools for agents."""

from typing import Annotated, Any

from langchain_core.tools import tool

from .base import api_client


@tool
async def list_resource_inventory() -> dict[str, Any]:
    """List available resources: servers, configured projects, API keys.

    Use when user asks about resource status or what's available.
    This is informational only - NOT for flow logic.

    Returns:
        - servers: list of managed servers with capacity
        - projects_with_secrets: count of projects that have secrets configured
        - total_projects: total project count
    """
    # Get servers
    try:
        servers = await api_client.get("/api/servers/?is_managed=true")
    except Exception:
        servers = []

    # Get projects
    try:
        projects = await api_client.get("/projects/")
    except Exception:
        projects = []

    # Count projects with secrets
    projects_with_secrets = sum(
        1 for p in projects
        if (p.get("config") or {}).get("secrets")
    )

    # Summarize servers
    server_summary = [
        {
            "handle": s.get("handle"),
            "status": s.get("status"),
            "available_ram_mb": s.get("capacity_ram_mb", 0) - s.get("used_ram_mb", 0),
        }
        for s in servers
    ]

    return {
        "servers": server_summary,
        "total_servers": len(servers),
        "total_projects": len(projects),
        "projects_with_secrets": projects_with_secrets,
        "projects_ready_to_deploy": sum(
            1 for p in projects if p.get("status") == "setup_required"
        ),
    }


@tool
async def create_service_deployment(
    project_id: Annotated[str, "Project ID"],
    service_name: Annotated[str, "Service name"],
    server_handle: Annotated[str, "Server handle"],
    port: Annotated[int, "Allocated port"],
    deployment_info: Annotated[dict, "Deployment configuration (repo_url, branch, etc.)"],
) -> dict[str, Any]:
    """Create a service deployment record after successful deployment.

    This tracks the deployment for future recovery needs.
    """
    payload = {
        "project_id": project_id,
        "service_name": service_name,
        "server_handle": server_handle,
        "port": port,
        "status": "running",
        "deployment_info": deployment_info,
    }

    return await api_client.post("/api/service-deployments/", json=payload)
