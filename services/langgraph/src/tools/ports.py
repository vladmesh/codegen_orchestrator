"""Port allocation tools for agents."""

from http import HTTPStatus
from typing import Annotated, Any

from langchain_core.tools import tool

from .base import api_client
from ..schemas.tools import PortAllocationResult


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
    
    allocation = await api_client.post(
        f"/api/servers/{server_handle}/ports", json=payload
    )

    # Fetch server info to ensure downstream nodes (DevOps) have the IP
    try:
        resp_server = await api_client.get_raw(f"/api/servers/{server_handle}")
        if resp_server.status_code == HTTPStatus.OK:
            server_info = resp_server.json()
            allocation["server_ip"] = server_info.get("public_ip") or server_info.get("host")
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
    ports_data = await api_client.get(f"/api/servers/{server_handle}/ports")
    allocated = {p["port"] for p in ports_data}

    port = start_port
    while port in allocated:
        port += 1
    return port
