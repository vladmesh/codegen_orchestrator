"""Database Tools for agents - access internal DB via API."""

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
    modules: Annotated[
        list[str], "Modules to generate: backend, tg_bot, notifications, frontend"
    ],
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
async def list_managed_servers() -> list[dict[str, Any]]:
    """List all active, managed servers available for deployment.

    Returns servers with their capacity (RAM/Disk) and current usage.
    Only returns servers that are managed (not ghost/personal) and active.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        url = f"{INTERNAL_API_URL}/api/servers?is_managed=true&status=active"
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
        url = f"{INTERNAL_API_URL}/api/servers?is_managed=true&status=active"
        resp = await client.get(url)
        resp.raise_for_status()
        servers = resp.json()

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
            if resp_server.status_code == 200:
                server_info = resp_server.json()
                allocation["server_ip"] = server_info.get("public_ip") or server_info.get("host")
        except Exception:
            # Don't fail the allocation if we can't get the IP, but it might cause issues later
            pass

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
