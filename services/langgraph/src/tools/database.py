"""Internal Database Tools."""

import os
import httpx
from langchain_core.tools import tool
from typing import Annotated, Dict, Any, List

INTERNAL_API_URL = os.getenv("API_URL", "http://api:8000")


@tool
async def register_server_in_db(
    handle: Annotated[str, "Unique handle/name for the server"],
    host: Annotated[str, "Hostname or IP address"],
    public_ip: Annotated[str, "Public IP address"],
    ssh_key: Annotated[str, "Private SSH key (will be encrypted)"],
    ssh_user: Annotated[str, "SSH username"] = "root",
    labels: Annotated[Dict[str, Any], "Metadata labels"] = {}
) -> Dict[str, Any]:
    """Register a new server in the internal database system."""
    payload = {
        "handle": handle,
        "host": host,
        "public_ip": public_ip,
        "ssh_key": ssh_key,
        "ssh_user": ssh_user,
        "labels": labels,
        # Default capacity values
        "capacity_cpu": 1,
        "capacity_ram_mb": 1024
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{INTERNAL_API_URL}/api/servers/", json=payload)
        resp.raise_for_status()
        return resp.json()


@tool
async def allocate_port(
    server_handle: Annotated[str, "Handle of the server"],
    port: Annotated[int, "Port number to allocate"],
    service_name: Annotated[str, "Name of the service utilizing the port"],
    project_id: Annotated[str, "ID of the project owning the service"]
) -> Dict[str, Any]:
    """Allocate a specific port on a server in the database to prevent collisions."""
    payload = {
        "server_handle": server_handle,
        "port": port,
        "service_name": service_name,
        "project_id": project_id
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{INTERNAL_API_URL}/api/servers/{server_handle}/ports", 
            json=payload
        )
        resp.raise_for_status()
        return resp.json()


@tool
async def list_managed_servers() -> List[Dict[str, Any]]:
    """List all active, managed servers available for deployment."""
    async with httpx.AsyncClient() as client:
        url = f"{INTERNAL_API_URL}/api/servers?is_managed=true&status=active"
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
