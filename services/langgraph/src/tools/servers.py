"""Server management tools for agents."""

from typing import Annotated

from langchain_core.tools import tool

from ..schemas.tools import ApplicationInfo, ServerInfo, ServerSearchResult
from .base import api_client


@tool
async def list_managed_servers() -> list[ServerInfo]:
    """List all managed servers available for deployment.

    Returns servers with their capacity (RAM/Disk) and current usage.
    Only returns servers that are managed (not ghost/personal).
    """
    servers = await api_client.list_servers(is_managed=True)
    return [ServerInfo(**s.model_dump()) for s in servers]


@tool
async def find_suitable_server(
    min_ram_mb: Annotated[int, "Minimum available RAM in MB required"],
    min_disk_mb: Annotated[int, "Minimum available disk space in MB required"] = 0,
) -> ServerSearchResult | None:
    """Find a server that has enough available resources.

    Searches managed servers for one with sufficient free RAM and disk.
    Returns the best match (most available resources) or None if no suitable server exists.

    Args:
        min_ram_mb: Minimum RAM needed (e.g., 1024 for 1GB)
        min_disk_mb: Minimum disk needed (e.g., 5120 for 5GB)

    Returns:
        Server details if found, None otherwise.
    """
    # Don't filter by status - include 'ready' and 'in_use' servers
    servers = await api_client.list_servers(is_managed=True)

    # Filter to only ready/in_use servers (active for deployment)
    servers = [s for s in servers if s.status in ("ready", "in_use")]

    # Filter by available resources
    suitable = []
    for srv in servers:
        available_ram = srv.capacity_ram_mb - srv.used_ram_mb
        available_disk = srv.capacity_disk_mb - srv.used_disk_mb

        if available_ram >= min_ram_mb and available_disk >= min_disk_mb:
            suitable.append(
                {
                    **srv.model_dump(),
                    "available_ram_mb": available_ram,
                    "available_disk_mb": available_disk,
                }
            )

    if not suitable:
        return None

    # Return the one with most available RAM
    best = max(suitable, key=lambda s: s["available_ram_mb"])
    return ServerSearchResult(**best)


@tool
async def get_server_info(
    handle: Annotated[str, "Handle/name of the server (e.g., 'vps-265601')"],
) -> ServerInfo:
    """Get detailed information about a specific server.

    Returns capacity, usage, IP, OS, and other details for the server.
    """
    server = await api_client.get_server(handle)
    return ServerInfo(**server.model_dump())


@tool
async def update_server_status(
    handle: Annotated[str, "Server handle"],
    status: Annotated[str, "New status (e.g., 'provisioning', 'ready', 'error')"],
) -> ServerInfo:
    """Update server status in database.

    Used by provisioner to track provisioning progress.
    """
    resp = await api_client.update_server(handle, {"status": status})
    return ServerInfo(**resp)


@tool
async def get_services_on_server(
    server_handle: Annotated[str, "Server handle"],
) -> list[ApplicationInfo]:
    """Get all applications running on a server.

    Returns application records with status and port information.
    """
    resp = await api_client.get_server_services(server_handle)
    return [ApplicationInfo(**app) for app in resp]
