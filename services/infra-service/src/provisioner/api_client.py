"""API client for provisioner - communicates with the API service."""

from shared.contracts.dto.server import ServerDTO
from shared.log_config import get_logger

from ..clients.api import api_client

logger = get_logger(__name__)


async def get_server_info(server_handle: str) -> ServerDTO | None:
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
        current_labels = dict(current.labels or {})
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


async def save_server_ssh_key(server_handle: str, ssh_key: str) -> bool:
    """Save SSH private key to server record via API (encrypted at rest).

    Args:
        server_handle: Server handle
        ssh_key: Raw SSH private key content

    Returns:
        True if successful
    """
    try:
        await api_client.update_server(server_handle, {"ssh_key": ssh_key})
        logger.info("api_server_ssh_key_saved", server_handle=server_handle)
        return True
    except Exception as e:
        logger.error(
            "api_server_ssh_key_save_failed",
            server_handle=server_handle,
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


async def reserve_provisioning_attempt(
    server_handle: str, max_attempts: int
) -> tuple[int, str] | None:
    """Reserve an attempt and return its number and episode id, or None at the limit."""
    reservation = await api_client.reserve_provisioning_attempt(
        server_handle,
        max_attempts=max_attempts,
    )
    if not reservation.reserved:
        return None
    episode_id = reservation.episode_id
    if episode_id is None:
        raise RuntimeError("Provisioning attempt reservation has no episode id")
    return reservation.provisioning_attempts, episode_id


async def reset_provisioning_attempts(
    server_handle: str, attempt_number: int, episode_id: str
) -> bool:
    """Atomically clear attempts and mark ready if this attempt is still current."""
    result = await api_client.reset_provisioning_attempts(server_handle, attempt_number, episode_id)
    return result.reset
