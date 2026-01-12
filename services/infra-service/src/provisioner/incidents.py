"""Incident management for provisioner."""

from datetime import datetime

from shared.logging_config import get_logger
from src.clients.api import api_client

logger = get_logger(__name__)


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
    try:
        await api_client.create_incident(
            {
                "server_handle": server_handle,
                "incident_type": incident_type,
                "details": details,
                "affected_services": affected_services or [],
            }
        )
        logger.info(
            "incident_created",
            server_handle=server_handle,
            incident_type=incident_type,
        )
        return True
    except Exception as e:
        logger.error(
            "incident_create_failed",
            server_handle=server_handle,
            incident_type=incident_type,
            error=str(e),
        )
        return False


async def resolve_active_incidents(server_handle: str) -> bool:
    """Resolve all active incidents for a server after successful recovery.

    Args:
        server_handle: Server handle

    Returns:
        True if successful
    """
    try:
        incidents: list[dict] = []
        for status in ["detected", "recovering"]:
            incidents.extend(
                await api_client.list_incidents({"server_handle": server_handle, "status": status})
            )

        if not incidents:
            logger.debug("incident_resolve_skipped", server_handle=server_handle)
            return True

        resolved_at = datetime.utcnow().isoformat()

        for incident in incidents:
            incident_id = incident.get("id")
            await api_client.update_incident(
                incident_id,
                {
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
