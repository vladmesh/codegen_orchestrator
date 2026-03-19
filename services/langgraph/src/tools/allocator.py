"""Shared resource allocation logic.

This module provides a reusable function for allocating server resources
(finding a suitable server and allocating ports) for a project.

Used by:
- ResourceAllocatorNode (engineering flow)
- deploy_worker (deploy flow)
"""

import structlog

from shared.contracts.dto.server import ServerDTO

from ..clients.api import api_client
from ..schemas.api_types import AllocationInfo

logger = structlog.get_logger(__name__)


class AllocationError(Exception):
    """Raised when resource allocation fails."""

    pass


async def ensure_project_allocations(
    project_id: str,
    repo_id: str,
    service_name: str,
    modules: list[str] | None = None,
    min_ram_mb: int = 512,
    min_disk_mb: int = 1024,
) -> dict[str, dict]:
    """Ensure a project has resource allocations, creating them if needed.

    This is the single source of truth for allocation logic. It:
    1. Gets or creates an Application for the repo+server
    2. Checks if allocations already exist for the application
    3. If yes, returns existing allocations
    4. If no, finds a suitable server and allocates ports

    Args:
        project_id: Project ID (for finding server/repo)
        repo_id: Repository ID for the Application
        service_name: Human-readable name (e.g. "fortune-teller-bot")
        modules: List of modules needing ports (default: ["backend"])
        min_ram_mb: Minimum RAM required
        min_disk_mb: Minimum disk required

    Returns:
        Dict of allocations keyed by "server_handle:port"

    Raises:
        AllocationError: If allocation fails
    """
    if modules is None:
        modules = ["backend"]

    # Find suitable server first (needed for Application creation)
    server = await _find_suitable_server(min_ram_mb, min_disk_mb)
    if not server:
        raise AllocationError("No suitable server found with enough resources")

    server_handle = server.handle
    server_ip = server.public_ip

    # Get or create Application
    app = await api_client.get_or_create_application(
        repo_id=repo_id,
        server_handle=server_handle,
        service_name=service_name,
    )
    application_id = app["id"]

    # Check for existing allocations on this application
    existing: list[AllocationInfo] = await api_client.get_application_allocations(application_id)
    if existing:
        logger.info(
            "allocations_already_exist",
            application_id=application_id,
            count=len(existing),
        )
        result = {}
        for alloc in existing:
            alloc_server = alloc["server_handle"]
            port = alloc["port"]
            key = f"{alloc_server}:{port}"

            alloc_ip = alloc.get("server_ip")
            if not alloc_ip:
                srv: ServerDTO = await api_client.get_server(alloc_server)
                alloc_ip = srv.public_ip

            result[key] = {
                "port": port,
                "server_handle": alloc_server,
                "server_ip": alloc_ip,
                "service_name": alloc.get("service_name"),
                "application_id": application_id,
            }
        return result

    # No existing allocations - create new ones
    logger.info(
        "allocating_resources",
        application_id=application_id,
        modules=modules,
        min_ram_mb=min_ram_mb,
    )

    # Allocate port for each module atomically
    allocated = {}
    for module in modules:
        alloc_result = await api_client.allocate_next_port(
            server_handle,
            {
                "service_name": module,
                "application_id": application_id,
            },
        )
        port = alloc_result["port"]
        key = f"{server_handle}:{port}"
        allocated[key] = {
            "port": port,
            "server_handle": server_handle,
            "server_ip": server_ip,
            "service_name": module,
            "application_id": application_id,
        }

        logger.info(
            "port_allocated",
            application_id=application_id,
            module=module,
            server=server_handle,
            port=port,
        )

    return allocated


async def _find_suitable_server(min_ram_mb: int, min_disk_mb: int) -> ServerDTO | None:
    """Find a managed server with enough resources.

    Note: We don't check used_ram_mb because that reflects actual system RAM usage
    (OS + Docker), not project allocations. A fresh Ubuntu + Docker uses ~3.5GB.
    Instead, we just check that the server is ready and has enough total capacity.
    """
    servers = await api_client.list_servers(is_managed=True)

    # Filter to only active/ready/in_use servers
    servers = [s for s in servers if s.status in ("active", "ready", "in_use")]

    # Filter by total capacity (not used - that's system RAM, not allocations)
    suitable = []
    for srv in servers:
        # Just check total capacity is sufficient for the project
        if srv.capacity_ram_mb >= min_ram_mb and srv.capacity_disk_mb >= min_disk_mb:
            suitable.append(srv)

    if not suitable:
        return None

    # Return the one with most capacity
    return max(suitable, key=lambda s: s.capacity_ram_mb)
