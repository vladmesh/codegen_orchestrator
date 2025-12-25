"""Server management tools for agents."""

from typing import Annotated, Any

from langchain_core.tools import tool

from .base import api_client


@tool
async def list_managed_servers() -> list[dict[str, Any]]:
    """List all managed servers available for deployment.

    Returns servers with their capacity (RAM/Disk) and current usage.
    Only returns servers that are managed (not ghost/personal).
    """
    return await api_client.get("/api/servers/?is_managed=true")


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
    # Don't filter by status - include 'ready' and 'in_use' servers
    servers = await api_client.get("/api/servers?is_managed=true")
    
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
    return await api_client.get(f"/api/servers/{handle}")


@tool
async def update_server_status(
    handle: Annotated[str, "Server handle"],
    status: Annotated[str, "New status (e.g., 'provisioning', 'ready', 'error')"],
) -> dict[str, Any]:
    """Update server status in database.

    Used by provisioner to track provisioning progress.
    """
    return await api_client.patch(f"/api/servers/{handle}", json={"status": status})


@tool
async def get_services_on_server(
    server_handle: Annotated[str, "Server handle"],
) -> list[dict[str, Any]]:
    """Get all active service deployments on a server.

    Returns service deployment records with deployment_info for redeployment after recovery.
    """
    return await api_client.get(f"/api/servers/{server_handle}/services")
