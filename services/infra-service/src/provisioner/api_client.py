"""API client for provisioner - communicates with the API service."""

from shared.contracts.dto.server import ServerDTO
from shared.log_config import get_logger

from ..clients.api import DeploymentRecord, api_client

logger = get_logger(__name__)


async def get_server_info(server_handle: str) -> ServerDTO:
    """Fetch typed server information from the API."""
    return await api_client.get_server(server_handle)


async def update_server_status(server_handle: str, status: str) -> None:
    """Update server status or propagate the API error."""
    await api_client.update_server(server_handle, {"status": status})
    logger.info("api_server_status_updated", server_handle=server_handle, status=status)


async def update_server_labels(server_handle: str, labels: dict) -> None:
    """Update server labels in database via API.

    Args:
        server_handle: Server handle
        labels: New labels dict (will be merged with existing)

    """
    current = await api_client.get_server(server_handle)
    final_labels = dict(current.labels or {}) | labels
    await api_client.update_server(server_handle, {"labels": final_labels})
    logger.info("api_server_labels_updated", server_handle=server_handle, labels=final_labels)


async def save_server_ssh_key(server_handle: str, ssh_key: str) -> None:
    """Save SSH private key to server record via API (encrypted at rest).

    Args:
        server_handle: Server handle
        ssh_key: Raw SSH private key content

    """
    await api_client.update_server(server_handle, {"ssh_key": ssh_key})
    logger.info("api_server_ssh_key_saved", server_handle=server_handle)


async def get_services_on_server(server_handle: str) -> list[DeploymentRecord]:
    """Get services deployed on a server for redeployment.

    Args:
        server_handle: Server handle

    """
    return await api_client.get_server_services(server_handle)


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
