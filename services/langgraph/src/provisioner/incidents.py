"""Incident management for provisioner."""

from datetime import datetime
from http import HTTPStatus
import os

import httpx

from shared.logging_config import get_logger

logger = get_logger(__name__)


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
            logger.info(
                "incident_created",
                server_handle=server_handle,
                incident_type=incident_type,
            )
            return True
    except Exception as e:
        logger.error("incident_create_failed", error=str(e))
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
                if resp.status_code == HTTPStatus.OK:
                    incidents.extend(resp.json())

            if not incidents:
                logger.debug("incident_resolve_skipped", server_handle=server_handle)
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
                logger.info(
                    "incident_resolved",
                    incident_id=incident_id,
                    server_handle=server_handle,
                )

            return True

    except Exception as e:
        logger.error(
            "incident_resolve_failed",
            server_handle=server_handle,
            error=str(e),
        )
        return False
