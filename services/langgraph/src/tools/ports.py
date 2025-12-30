"""Port allocation tools for agents."""

from typing import Annotated

from langchain_core.tools import tool

from ..schemas.api_types import AllocationInfo, ServerInfo, get_server_ip
from ..schemas.tools import PortAllocationResult
from .base import api_client


@tool
async def allocate_port(
    server_handle: Annotated[str, "Handle of the server"],
    port: Annotated[int, "Port number to allocate"],
    service_name: Annotated[str, "Name of the service utilizing the port"],
    project_id: Annotated[str, "ID of the project owning the service"],
) -> PortAllocationResult:
    """Allocate a specific port on a server to prevent collisions.

    Reserves a port for a service. If port is already taken, will error.
    """
    payload = {
        "server_handle": server_handle,
        "port": port,
        "service_name": service_name,
        "project_id": project_id,
    }

    allocation = await api_client.allocate_server_port(server_handle, payload)

    # Fetch server info to ensure downstream nodes (DevOps) have the IP
    try:
        server_info: ServerInfo = await api_client.get_server(server_handle)
        allocation["server_ip"] = get_server_ip(server_info)
    except Exception as e:
        # allocate_port MUST return server_ip for DevOps to work
        raise RuntimeError(f"Failed to fetch server IP for {server_handle}: {e}") from e

    return PortAllocationResult(**allocation)


@tool
async def get_next_available_port(
    server_handle: Annotated[str, "Handle of the server"],
    start_port: Annotated[int, "Starting port to search from"] = 8000,
) -> int:
    """Find the next available port on a server.

    Searches from start_port upwards to find an unallocated port.
    """
    ports_data: list[AllocationInfo] = await api_client.list_server_ports(server_handle)
    allocated = {p["port"] for p in ports_data}

    port = start_port
    while port in allocated:
        port += 1
    return port
