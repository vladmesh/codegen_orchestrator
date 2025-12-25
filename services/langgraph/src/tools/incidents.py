"""Incident tracking tools for agents."""

from typing import Annotated, Any

from langchain_core.tools import tool

from .base import api_client


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

    return await api_client.post("/api/incidents/", json=payload)


@tool
async def list_active_incidents() -> list[dict[str, Any]]:
    """List all active incidents (detected or recovering).

    Use this to check for any ongoing server or service issues.
    Returns incidents that need attention - servers that are down,
    provisioning failures, or services that have crashed.

    The Product Owner should call this proactively to alert users
    about any ongoing issues that may affect their projects.
    """
    return await api_client.get("/api/incidents/active")
