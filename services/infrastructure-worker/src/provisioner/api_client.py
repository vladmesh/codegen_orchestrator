"""API client for provisioner - communicates with the API service."""

from shared.logging_config import get_logger

from ..clients.api import api_client

logger = get_logger(__name__)


async def get_server_info(server_handle: str) -> dict | None:
    """Fetch server info from API.

    Args:
        server_handle: Server handle

    Returns:
        Server info dict or None on error
    """
    try:
        return await api_client.get_server(server_handle)
    except Exception as e:
        logger.error(
            "api_server_info_failed",
            server_handle=server_handle,
            error=str(e),
        )
        return None


async def update_server_status(server_handle: str, status: str) -> bool:
    """Update server status in database via API.

    Args:
        server_handle: Server handle
        status: New status

    Returns:
        True if successful
    """
    try:
        await api_client.update_server(server_handle, {"status": status})
        logger.info(
            "api_server_status_updated",
            server_handle=server_handle,
            status=status,
        )
        return True
    except Exception as e:
        logger.error(
            "api_server_status_update_failed",
            server_handle=server_handle,
            status=status,
            error=str(e),
        )
        return False


async def update_server_labels(server_handle: str, labels: dict) -> bool:
    """Update server labels in database via API.

    Args:
        server_handle: Server handle
        labels: New labels dict (will be merged with existing)

    Returns:
        True if successful
    """
    try:
        current = await api_client.get_server(server_handle)
        current_labels = current.get("labels", {}) or {}
        current_labels.update(labels)
        final_labels = current_labels

        await api_client.update_server(server_handle, {"labels": final_labels})
        logger.info(
            "api_server_labels_updated",
            server_handle=server_handle,
            labels=final_labels,
        )
        return True
    except Exception as e:
        logger.error(
            "api_server_labels_update_failed",
            server_handle=server_handle,
            labels=labels,
            error=str(e),
        )
        return False


async def get_services_on_server(server_handle: str) -> list[dict]:
    """Get services deployed on a server for redeployment.

    Args:
        server_handle: Server handle

    Returns:
        List of service deployment records
    """
    try:
        return await api_client.get_server_services(server_handle)
    except Exception as e:
        logger.error(
            "api_server_services_fetch_failed",
            server_handle=server_handle,
            error=str(e),
        )
        return []


async def increment_provisioning_attempts(server_handle: str) -> bool:
    """Increment provisioning attempts counter for a server.

    Args:
        server_handle: Server handle

    Returns:
        True if successful
    """
    try:
        current = await api_client.get_server(server_handle)
        attempts = current.get("provisioning_attempts", 0)
        await api_client.update_server(
            server_handle,
            {"provisioning_attempts": attempts + 1},
        )
        return True
    except Exception as e:
        logger.error(
            "api_provisioning_attempt_increment_failed",
            server_handle=server_handle,
            error=str(e),
        )
        return False
