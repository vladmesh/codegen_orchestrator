"""Time4VPS Tools for LangChain."""

from typing import Annotated, Any

from langchain_core.tools import tool

from shared.clients.time4vps import Time4VPSClient

client = Time4VPSClient()


@tool
async def list_servers() -> list[dict[str, Any]]:
    """List all available VPS servers from Time4VPS account.
    Use this to check current infrastructure inventory."""
    return await client.get_servers()


@tool
async def get_server_details(
    server_id: Annotated[int, "The ID of the server to inspect"],
) -> dict[str, Any]:
    """Get detailed specification (IP, resources, OS) of a specific server."""
    return await client.get_server_details(server_id)


@tool
async def reinstall_server(
    server_id: Annotated[int, "ID of the server to reinstall"],
    os_name: Annotated[str, "Operating system template name (e.g., ubuntu-22.04-x86_64)"],
    ssh_key: Annotated[str, "Public SSH key to inject into authorized_keys"],
) -> dict[str, Any]:
    """Reinstall a server with a specific OS and inject an SSH key.
    WARNING: This wipes all data on the server!"""
    return await client.reinstall_server(server_id, os_name, ssh_key)


@tool
async def list_dns_zones() -> dict[str, Any]:
    """List all DNS zones managed in Time4VPS. Returns domain_ids needed for record creation."""
    return await client.get_dns_zones()


# Note: Order and DNS Add Record can be added later if needed by current scenarios.
# For now, listing and reinstalling (provisioning) are key.
