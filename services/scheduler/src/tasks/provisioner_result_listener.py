"""Provisioner Result Listener.

Listens to provisioner:results stream and updates server status in DB via API.
Notifies admins on provisioning failures.
"""

import httpx
import structlog

from shared.contracts.dto.server import ServerStatus, ServerUpdate
from shared.contracts.queues.provisioner import ProvisionerResult
from shared.notifications import notify_admins
from src.clients.api import api_client

logger = structlog.get_logger(__name__)


async def process_provisioner_result(result: ProvisionerResult) -> None:
    """Process a single provisioner result and update server status.

    Args:
        result: ProvisionerResult from infra-service
    """
    server_handle = result.server_handle
    log = logger.bind(
        server_handle=server_handle,
        request_id=result.request_id,
        status=result.status,
    )

    log.info("processing_provisioner_result")

    if result.status == "success":
        await _handle_success(result, log)
    elif result.status in ("failed", "error"):
        await _handle_failure(result, log)
    else:
        log.warning("unknown_provisioner_status", received_status=result.status)


async def _handle_success(result: ProvisionerResult, log) -> None:
    """Handle successful provisioning - update server to active."""
    try:
        update = ServerUpdate(status=ServerStatus.ACTIVE)

        await api_client.update_server(result.server_handle, update)

        log.info(
            "server_status_updated",
            new_status=ServerStatus.ACTIVE,
            services_redeployed=result.services_redeployed,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == httpx.codes.NOT_FOUND:
            log.warning("server_not_found_in_api", server_handle=result.server_handle)
        else:
            log.error(
                "api_update_failed",
                status_code=e.response.status_code,
                error=str(e),
            )


async def _handle_failure(result: ProvisionerResult, log) -> None:
    """Handle failed provisioning - update server to unreachable and notify admins."""
    errors_str = ", ".join(result.errors) if result.errors else "Unknown error"

    try:
        update = ServerUpdate(status=ServerStatus.UNREACHABLE)

        await api_client.update_server(result.server_handle, update)

        log.info(
            "server_status_updated",
            new_status=ServerStatus.UNREACHABLE,
            errors=result.errors,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == httpx.codes.NOT_FOUND:
            log.warning("server_not_found_in_api", server_handle=result.server_handle)
        else:
            log.error(
                "api_update_failed",
                status_code=e.response.status_code,
                error=str(e),
            )

    # Notify admins about provisioning failure
    message = f"Provisioning failed for server `{result.server_handle}`\n" f"Errors: {errors_str}"
    await notify_admins(message, level="error")
    log.info("admins_notified_about_failure")
