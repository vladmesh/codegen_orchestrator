"""API client for provisioner - communicates with the API service."""

from http import HTTPStatus

import httpx

from shared.contracts.dto.server import (
    ProvisioningFinalization,
    ProvisioningFinalizationDisposition,
    ServerDTO,
    TargetReadinessRead,
    TargetReadinessReport,
)
from shared.log_config import get_logger
from shared.qa_identity import QA_SSH_USER, QA_SSH_USER_LABEL

from ..clients.api import DeploymentRecord, api_client

logger = get_logger(__name__)


class TargetReadinessSupersededError(RuntimeError):
    """The API refused a verdict: the row's identity or the profile changed meanwhile.

    Nothing was recorded. It is neither a ready nor a not-ready fact about the
    target as it is now, so it is never reported as one.
    """


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


async def list_managed_servers() -> list[ServerDTO]:
    """Every server row the platform manages."""
    return await api_client.list_servers(is_managed=True)


async def report_target_readiness(
    server_handle: str, report: TargetReadinessReport
) -> TargetReadinessRead:
    """Apply one readiness verdict: receipt and repair, or incident and park.

    Raises:
        TargetReadinessSupersededError: the API refused the verdict because the
            row's connection identity or the current profile changed meanwhile.
    """
    try:
        applied = await api_client.report_target_readiness(server_handle, report)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != HTTPStatus.CONFLICT:
            raise
        raise TargetReadinessSupersededError(f"{server_handle}: {exc.response.text[:300]}") from exc
    logger.info(
        "api_target_readiness_reported",
        server_handle=server_handle,
        ready=applied.ready,
        status=applied.status.value,
        phase=report.phase.value if report.phase else None,
        incident_id=applied.incident_id,
    )
    return applied


async def record_qa_identity(server_handle: str) -> None:
    """Record the QA identity on a host that was provisioned before it existed."""
    await update_server_labels(server_handle, {QA_SSH_USER_LABEL: QA_SSH_USER})


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


async def finalize_provisioning(
    server_handle: str, finalization: ProvisioningFinalization
) -> ProvisioningFinalizationDisposition:
    """Commit one provisioning success through the API's sole finalizer."""
    result = await api_client.finalize_provisioning(server_handle, finalization)
    logger.info(
        "api_provisioning_finalized",
        server_handle=server_handle,
        disposition=result.disposition.value,
        reason=result.reason,
    )
    return result.disposition
