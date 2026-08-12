"""API client for provisioner - communicates with the API service."""

from shared.contracts.dto.server import ServerDTO
from shared.log_config import get_logger
from shared.qa_identity import QA_SSH_USER, QA_SSH_USER_LABEL, provisioning_complete_labels

from ..clients.api import DeploymentRecord, api_client

logger = get_logger(__name__)


async def get_server_info(server_handle: str) -> ServerDTO:
    """Fetch typed server information from the API."""
    return await api_client.get_server(server_handle)


async def get_server_ssh_key(server_handle: str) -> str | None:
    """Fetch the decrypted SSH private key stored for a server."""
    return await api_client.get_server_ssh_key(server_handle)


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


async def mark_provisioning_complete(server_handle: str) -> None:
    """Record a finished software phase, and the QA identity that phase created.

    One write, from one function, because the two facts are one fact. The
    software playbook is what creates the QA account; `provisioning_phase`
    reaching `complete` is what says that playbook succeeded. If the identity
    were recorded anywhere else — a later step, a second call site — there would
    be a window in which a host reads as fully provisioned and lends no identity
    to a QA run, and the QA runtime would have to guess which of the two it was
    looking at.
    """
    await update_server_labels(server_handle, provisioning_complete_labels())


async def record_qa_identity(server_handle: str) -> None:
    """Record the QA identity on a host that was provisioned before it existed.

    The retrofit path's half of :func:`mark_provisioning_complete`: the phase is
    already complete on these hosts and is not re-run, so only the identity is
    written — and only after the playbook that creates the account succeeded.
    """
    await update_server_labels(server_handle, {QA_SSH_USER_LABEL: QA_SSH_USER})


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
    """Atomically clear attempts and mark ready if this attempt is still current.

    This endpoint is the single owner of the terminal READY status: the counter
    reset and the status write happen in one conditional UPDATE, so a superseded
    attempt can never mark a server that a newer episode already owns.
    """
    result = await api_client.reset_provisioning_attempts(server_handle, attempt_number, episode_id)
    return result.reset
