"""Database Tools for agents - access internal DB via API."""

from http import HTTPStatus
import os
from typing import Annotated, Any
import uuid

import httpx
from langchain_core.tools import tool

INTERNAL_API_URL = os.getenv("API_URL", "http://api:8000")


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

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.post(f"{INTERNAL_API_URL}/projects/", json=payload)
        resp.raise_for_status()
        return resp.json()


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
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(f"{INTERNAL_API_URL}/projects/", params=params)
        resp.raise_for_status()
        return resp.json()


@tool
async def get_project_status(
    project_id: Annotated[str, "Project ID"],
) -> dict[str, Any]:
    """Get a single project's status and metadata."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(f"{INTERNAL_API_URL}/projects/{project_id}")
        resp.raise_for_status()
        return resp.json()


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
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # First verify project exists
        resp = await client.get(f"{INTERNAL_API_URL}/projects/{project_id}")
        if resp.status_code == 404:
            return {"error": f"Project {project_id} not found"}
        resp.raise_for_status()
        project = resp.json()

        # Update status to maintenance
        resp = await client.patch(
            f"{INTERNAL_API_URL}/projects/{project_id}",
            json={
                "status": "maintenance",
                "config": {
                    **project.get("config", {}),
                    "maintenance_request": update_description,
                },
            },
        )
        resp.raise_for_status()
        return resp.json()


@tool
async def list_managed_servers() -> list[dict[str, Any]]:
    """List all managed servers available for deployment.

    Returns servers with their capacity (RAM/Disk) and current usage.
    Only returns servers that are managed (not ghost/personal).
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        url = f"{INTERNAL_API_URL}/api/servers/?is_managed=true"
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


@tool
async def find_suitable_server(
    min_ram_mb: Annotated[int, "Minimum available RAM in MB required"],
    min_disk_mb: Annotated[int, "Minimum available disk space in MB required"] = 0,
) -> dict[str, Any] | None:
    """Find a server that has enough available resources.

    Searches managed servers for one with sufficient free RAM and disk.
    Returns the best match (most available resources) or None if no suitable server exists.

    Args:
        min_ram_mb: Minimum RAM needed (e.g., 1024 for 1GB)
        min_disk_mb: Minimum disk needed (e.g., 5120 for 5GB)

    Returns:
        Server details if found, None otherwise.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Don't filter by status - include 'ready' and 'in_use' servers
        url = f"{INTERNAL_API_URL}/api/servers?is_managed=true"
        resp = await client.get(url)
        resp.raise_for_status()
        servers = resp.json()
        # Filter to only ready/in_use servers (active for deployment)
        servers = [s for s in servers if s.get("status") in ("ready", "in_use")]

    # Filter by available resources
    suitable = []
    for srv in servers:
        available_ram = srv.get("capacity_ram_mb", 0) - srv.get("used_ram_mb", 0)
        available_disk = srv.get("capacity_disk_mb", 0) - srv.get("used_disk_mb", 0)

        if available_ram >= min_ram_mb and available_disk >= min_disk_mb:
            suitable.append(
                {
                    **srv,
                    "available_ram_mb": available_ram,
                    "available_disk_mb": available_disk,
                }
            )

    if not suitable:
        return None

    # Return the one with most available RAM
    return max(suitable, key=lambda s: s["available_ram_mb"])


@tool
async def get_server_info(
    handle: Annotated[str, "Handle/name of the server (e.g., 'vps-265601')"],
) -> dict[str, Any]:
    """Get detailed information about a specific server.

    Returns capacity, usage, IP, OS, and other details for the server.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(f"{INTERNAL_API_URL}/api/servers/{handle}")
        resp.raise_for_status()
        return resp.json()


@tool
async def allocate_port(
    server_handle: Annotated[str, "Handle of the server"],
    port: Annotated[int, "Port number to allocate"],
    service_name: Annotated[str, "Name of the service utilizing the port"],
    project_id: Annotated[str, "ID of the project owning the service"],
) -> dict[str, Any]:
    """Allocate a specific port on a server to prevent collisions.

    Reserves a port for a service. If port is already taken, will error.
    """
    payload = {
        "server_handle": server_handle,
        "port": port,
        "service_name": service_name,
        "project_id": project_id,
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.post(
            f"{INTERNAL_API_URL}/api/servers/{server_handle}/ports", json=payload
        )
        resp.raise_for_status()
        allocation = resp.json()

        # Fetch server info to ensure downstream nodes (DevOps) have the IP
        try:
            resp_server = await client.get(f"{INTERNAL_API_URL}/api/servers/{server_handle}")
            if resp_server.status_code == HTTPStatus.OK:
                server_info = resp_server.json()
                allocation["server_ip"] = server_info.get("public_ip") or server_info.get("host")
        except Exception as e:
            # allocate_port MUST return server_ip for DevOps to work
            raise RuntimeError(f"Failed to fetch server IP for {server_handle}: {e}") from e

        return allocation


@tool
async def get_next_available_port(
    server_handle: Annotated[str, "Handle of the server"],
    start_port: Annotated[int, "Starting port to search from"] = 8000,
) -> int:
    """Find the next available port on a server.

    Searches from start_port upwards to find an unallocated port.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(f"{INTERNAL_API_URL}/api/servers/{server_handle}/ports")
        resp.raise_for_status()
        allocated = {p["port"] for p in resp.json()}

    port = start_port
    while port in allocated:
        port += 1
    return port


@tool
async def update_server_status(
    handle: Annotated[str, "Server handle"],
    status: Annotated[str, "New status (e.g., 'provisioning', 'ready', 'error')"],
) -> dict[str, Any]:
    """Update server status in database.

    Used by provisioner to track provisioning progress.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.patch(
            f"{INTERNAL_API_URL}/api/servers/{handle}", json={"status": status}
        )
        resp.raise_for_status()
        return resp.json()


@tool
async def create_incident(
    server_handle: Annotated[str, "Server handle"],
    incident_type: Annotated[str, "Type of incident (e.g., 'provisioning_failed')"],
    details: Annotated[dict, "Incident details"] = None,
) -> dict[str, Any]:
    """Create an incident record for tracking issues.

    Used when provisioning or other operations fail.
    """
    if details is None:
        details = {}

    payload = {
        "server_handle": server_handle,
        "incident_type": incident_type,
        "details": details,
        "affected_services": [],
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.post(f"{INTERNAL_API_URL}/api/incidents/", json=payload)
        resp.raise_for_status()
        return resp.json()


@tool
async def list_active_incidents() -> list[dict[str, Any]]:
    """List all active incidents (detected or recovering).

    Use this to check for any ongoing server or service issues.
    Returns incidents that need attention - servers that are down,
    provisioning failures, or services that have crashed.

    The Product Owner should call this proactively to alert users
    about any ongoing issues that may affect their projects.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(f"{INTERNAL_API_URL}/api/incidents/active")
        resp.raise_for_status()
        return resp.json()


@tool
async def get_services_on_server(
    server_handle: Annotated[str, "Server handle"],
) -> list[dict[str, Any]]:
    """Get all active service deployments on a server.

    Returns service deployment records with deployment_info for redeployment after recovery.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(f"{INTERNAL_API_URL}/api/servers/{server_handle}/services")
        resp.raise_for_status()

        return resp.json()


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

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.post(f"{INTERNAL_API_URL}/api/service-deployments/", json=payload)
        resp.raise_for_status()
        return resp.json()


# =============================================================================
# Project Activation Tools (PO Supervisor)
# =============================================================================


def _parse_env_example(content: str) -> list[str]:
    """Parse .env.example and extract variable names."""
    variables = []
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            var_name = line.split("=")[0].strip()
            if var_name:
                variables.append(var_name)
    return variables


@tool
async def activate_project(
    project_id: Annotated[str, "Project ID to activate for deployment"],
) -> dict[str, Any]:
    """Activate a discovered project for deployment.

    Changes project status to 'setup_required' and inspects the repository
    to determine what secrets/configuration are needed.

    Use when user says "launch X" or "activate X" for a discovered project.

    Returns:
        - status: new project status
        - required_secrets: list of env vars from .env.example
        - missing_secrets: secrets not yet configured
        - has_docker_compose: whether project has docker-compose.yml
        - repo_info: repository info (full_name, html_url, clone_url)
    """
    # Import here to avoid circular dependency
    from .github import get_github_client

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Get current project
        resp = await client.get(f"{INTERNAL_API_URL}/projects/{project_id}")
        if resp.status_code == 404:
            return {"error": f"Project {project_id} not found"}
        resp.raise_for_status()
        project = resp.json()

        # Update status to setup_required
        resp = await client.patch(
            f"{INTERNAL_API_URL}/projects/{project_id}",
            json={"status": "setup_required"},
        )
        resp.raise_for_status()

        # Inspect repository
        inspection = await inspect_repository.ainvoke({"project_id": project_id})

        # Build repo_info for DevOps
        project_name = project.get("name", project_id)
        repo_info = None
        try:
            github = get_github_client()
            installation = await github.get_first_org_installation()
            org = installation["org"]
            repo_info = {
                "full_name": f"{org}/{project_name}",
                "html_url": f"https://github.com/{org}/{project_name}",
                "clone_url": f"https://github.com/{org}/{project_name}.git",
            }
        except Exception as e:
            # Non-fatal: DevOps will fail later with a clear message
            pass

        return {
            "project_id": project_id,
            "project_name": project_name,
            "status": "setup_required",
            "required_secrets": inspection.get("required_secrets", []),
            "missing_secrets": inspection.get("missing_secrets", []),
            "has_docker_compose": inspection.get("has_docker_compose", False),
            "repo_info": repo_info,
        }



@tool
async def inspect_repository(
    project_id: Annotated[str, "Project ID to inspect"],
) -> dict[str, Any]:
    """Inspect a project's repository to determine deployment requirements.

    Fetches .env.example to determine required secrets,
    checks for docker-compose.yml, and compares against configured secrets.

    Returns:
        - required_secrets: list of env var names from .env.example
        - missing_secrets: secrets that are not yet configured
        - has_docker_compose: whether docker-compose.yml exists
        - files: list of files in repo root
    """
    # Import here to avoid circular dependency
    from .github import get_github_client

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Get project to find repo info
        resp = await client.get(f"{INTERNAL_API_URL}/projects/{project_id}")
        if resp.status_code == 404:
            return {"error": f"Project {project_id} not found"}
        resp.raise_for_status()
        project = resp.json()

    # Determine repo owner/name from project name (convention: org/project_name)
    project_name = project.get("name", project_id)
    config = project.get("config", {}) or {}
    existing_secrets = config.get("secrets", {}) or {}

    # Try to get GitHub client and inspect repo
    try:
        github = get_github_client()
        installation = await github.get_first_org_installation()
        org = installation["org"]

        # List files in root
        files = await github.list_repo_files(org, project_name)

        # Check for .env.example
        env_content = await github.get_file_contents(org, project_name, ".env.example")
        required_secrets = _parse_env_example(env_content) if env_content else []

        # Determine missing secrets
        missing_secrets = [s for s in required_secrets if s not in existing_secrets]

        # Check for docker-compose.yml
        has_docker_compose = "docker-compose.yml" in files or "docker-compose.yaml" in files

        return {
            "project_id": project_id,
            "required_secrets": required_secrets,
            "missing_secrets": missing_secrets,
            "has_docker_compose": has_docker_compose,
            "files": files[:20],  # Limit to first 20 files
        }
    except Exception as e:
        return {
            "project_id": project_id,
            "error": f"Failed to inspect repository: {e}",
            "required_secrets": [],
            "missing_secrets": [],
            "has_docker_compose": False,
            "files": [],
        }


@tool
async def save_project_secret(
    project_id: Annotated[str, "Project ID"],
    key: Annotated[str, "Secret key name (e.g., TELEGRAM_TOKEN)"],
    value: Annotated[str, "Secret value"],
) -> dict[str, Any]:
    """Save a secret for a project's deployment.

    Stores the secret in project.config.secrets.
    Call this after user provides a required secret.

    Returns confirmation and updated missing secrets list.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Get current project
        resp = await client.get(f"{INTERNAL_API_URL}/projects/{project_id}")
        if resp.status_code == 404:
            return {"error": f"Project {project_id} not found"}
        resp.raise_for_status()
        project = resp.json()

        # Update secrets in config
        config = project.get("config", {}) or {}
        secrets = config.get("secrets", {}) or {}
        secrets[key] = value
        config["secrets"] = secrets

        # Save updated config
        resp = await client.patch(
            f"{INTERNAL_API_URL}/projects/{project_id}",
            json={"config": config},
        )
        resp.raise_for_status()

        # Re-inspect to get updated missing secrets
        inspection = await inspect_repository.ainvoke({"project_id": project_id})

        return {
            "saved": True,
            "key": key,
            "project_id": project_id,
            "missing_secrets": inspection.get("missing_secrets", []),
        }


@tool
async def check_ready_to_deploy(
    project_id: Annotated[str, "Project ID to check"],
) -> dict[str, Any]:
    """Check if a project has all requirements for deployment.

    Verifies all secrets are configured and repo is ready.
    If ready, returns ready=True and the deployment can proceed.

    Returns:
        - ready: bool - whether project can be deployed
        - missing: list of missing items
        - project_name: for reference
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Get project
        resp = await client.get(f"{INTERNAL_API_URL}/projects/{project_id}")
        if resp.status_code == 404:
            return {"error": f"Project {project_id} not found", "ready": False}
        resp.raise_for_status()
        project = resp.json()

    # Inspect repo for requirements
    inspection = await inspect_repository.ainvoke({"project_id": project_id})

    missing = inspection.get("missing_secrets", [])
    ready = len(missing) == 0

    return {
        "ready": ready,
        "project_id": project_id,
        "project_name": project.get("name"),
        "missing": missing,
        "has_docker_compose": inspection.get("has_docker_compose", False),
    }


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
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Get servers
        resp = await client.get(f"{INTERNAL_API_URL}/api/servers/?is_managed=true")
        servers = resp.json() if resp.status_code == 200 else []

        # Get projects
        resp = await client.get(f"{INTERNAL_API_URL}/projects/")
        projects = resp.json() if resp.status_code == 200 else []

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

