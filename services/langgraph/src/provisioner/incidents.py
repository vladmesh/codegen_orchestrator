"""Incident management for provisioner."""

import logging
import os
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


def _get_api_url() -> str:
    """Get API base URL."""
    return os.getenv("API_URL", "http://api:8000")


async def create_incident(
    server_handle: str,
    incident_type: str,
    details: dict,
    affected_services: list[str] | None = None,
) -> bool:
    """Create incident record in database.

    Args:
        server_handle: Server handle
        incident_type: Type of incident (e.g., 'provisioning_failed', 'ssh_unreachable')
        details: Incident details dict
        affected_services: Optional list of affected service names

    Returns:
        True if successful
    """
    api_url = _get_api_url()

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{api_url}/api/incidents/",
                json={
                    "server_handle": server_handle,
                    "incident_type": incident_type,
                    "details": details,
                    "affected_services": affected_services or [],
                },
            )
            resp.raise_for_status()
            logger.info(f"Created incident for server {server_handle}: {incident_type}")
            return True
    except Exception as e:
        logger.error(f"Failed to create incident: {e}")
        return False


async def resolve_active_incidents(server_handle: str) -> bool:
    """Resolve all active incidents for a server after successful recovery.

    Args:
        server_handle: Server handle

    Returns:
        True if successful
    """
    api_url = _get_api_url()

    try:
        async with httpx.AsyncClient() as client:
            # Get active incidents (detected + recovering)
            incidents = []

            for status in ["detected", "recovering"]:
                resp = await client.get(
                    f"{api_url}/api/incidents/",
                    params={"server_handle": server_handle, "status": status},
                )
                if resp.status_code == 200:
                    incidents.extend(resp.json())

            if not incidents:
                logger.debug(f"No active incidents to resolve for {server_handle}")
                return True

            resolved_at = datetime.utcnow().isoformat()

            for incident in incidents:
                incident_id = incident.get("id")
                await client.patch(
                    f"{api_url}/api/incidents/{incident_id}",
                    json={
                        "status": "resolved",
                        "resolved_at": resolved_at,
                    },
                )
                logger.info(f"Resolved incident #{incident_id} for server {server_handle}")

            return True

    except Exception as e:
        logger.error(f"Failed to resolve incidents for {server_handle}: {e}")
        return False
