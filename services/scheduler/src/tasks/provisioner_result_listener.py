"""Provisioner Result Listener.

Listens to provisioner:results stream and updates server status in DB via API.
Notifies admins on provisioning failures.
"""

import os

import httpx
from pydantic import ValidationError
import structlog

from shared.contracts.dto.server import ServerStatus, ServerUpdate
from shared.contracts.queues.provisioner import ProvisionerResult
from shared.contracts.vocab import ResultStatus
from shared.notifications import notify_admins_best_effort
from shared.queues import PROVISIONER_RESULTS, SCHEDULER_CONSUMER_GROUP
from shared.redis import RedisStreamClient
from src.clients.api import api_client

logger = structlog.get_logger(__name__)
CONSUMER_NAME = f"scheduler-infrastructure-{os.getpid()}"


async def provisioner_results_worker() -> None:
    """Consume provisioner results with restart-safe PEL reclamation."""
    client = RedisStreamClient()
    await client.connect()
    logger.info(
        "provisioner_results_worker_started", stream=PROVISIONER_RESULTS, consumer=CONSUMER_NAME
    )
    try:
        async for msg in client.consume(
            PROVISIONER_RESULTS,
            SCHEDULER_CONSUMER_GROUP,
            CONSUMER_NAME,
            auto_ack=False,
            claim_pending=True,
        ):
            if msg is None:
                continue
            try:
                await handle_provisioner_entry(client, msg)
            except Exception:
                logger.exception("provisioner_result_processing_error", entry_id=msg.message_id)
    finally:
        await client.close()
        logger.info("provisioner_results_worker_stopped")


async def handle_provisioner_entry(client, msg) -> None:
    """Validate, process, and ACK a single provisioner:results entry.

    A message that fails schema validation can never succeed on retry. Since the
    consumer reclaims pending (unacked) entries, leaving it unacked would poison
    the loop forever. So a validation failure is terminal: log it loudly as the
    human signal and ACK it away. Processing errors (e.g. a transient API call)
    propagate unacked so the entry stays in the PEL and gets retried.
    """
    try:
        result = ProvisionerResult.model_validate(msg.data)
    except ValidationError as e:
        logger.error(
            "provisioner_result_invalid_discarded",
            entry_id=msg.message_id,
            data=msg.data,
            error=str(e),
        )
        await client.ack(PROVISIONER_RESULTS, SCHEDULER_CONSUMER_GROUP, msg.message_id)
        return

    await process_provisioner_result(result)
    await client.ack(PROVISIONER_RESULTS, SCHEDULER_CONSUMER_GROUP, msg.message_id)


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

    if result.status == ResultStatus.SUCCESS:
        await _handle_success(result, log)
    elif result.status == ResultStatus.FAILED:
        await _handle_failure(result, log)
    elif result.status == ResultStatus.SUPERSEDED:
        # A stale job whose completion was superseded by a newer attempt for the
        # same server. The newer job owns the server state, so this is a no-op:
        # no status mutation, no failure notification.
        log.info("provisioner_result_superseded")
    else:
        log.warning("unknown_provisioner_status", received_status=result.status)


async def _handle_success(result: ProvisionerResult, log) -> None:
    """Observe successful provisioning without touching the terminal status.

    The success path owns the terminal status: it is written atomically, together
    with the attempt-counter reset, by the provisioning-attempts reset endpoint,
    and only while the episode is still current. This listener used to overwrite
    that READY with ACTIVE from the side, so it only logs now.
    """
    log.info(
        "provisioner_result_success_observed",
        services_redeployed=result.services_redeployed,
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

    message = f"Provisioning failed for server `{result.server_handle}`\nErrors: {errors_str}"
    await notify_admins_best_effort(
        message,
        level="error",
        component="provisioner_result_listener",
        server_handle=result.server_handle,
        request_id=result.request_id,
    )
