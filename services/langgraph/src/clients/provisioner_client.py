"""Client for infrastructure-worker via Redis queue.

Fire-and-forget: queues provisioning jobs to provisioner:queue.
Results are handled by infra-service → scheduler pipeline.
"""

from __future__ import annotations

import structlog

from shared.contracts.queues.provisioner import ProvisionerMessage
from shared.queues import PROVISIONER_QUEUE
from shared.redis_client import RedisStreamClient

logger = structlog.get_logger(__name__)


async def trigger_provisioning(
    server_handle: str,
    force_reinstall: bool = False,
    is_recovery: bool = False,
    correlation_id: str | None = None,
) -> str:
    """Queue a provisioning job and return the request_id.

    Args:
        server_handle: Server handle to provision
        force_reinstall: Force OS reinstall
        is_recovery: Whether this is incident recovery
        correlation_id: Optional correlation ID for tracing

    Returns:
        Request ID for tracing
    """
    msg = ProvisionerMessage(
        server_handle=server_handle,
        force_reinstall=force_reinstall,
        is_recovery=is_recovery,
    )
    if correlation_id:
        msg.correlation_id = correlation_id

    redis = RedisStreamClient()
    await redis.connect()

    try:
        await redis.publish_message(PROVISIONER_QUEUE, msg)
        logger.info(
            "provisioning_job_queued",
            request_id=msg.request_id,
            server_handle=server_handle,
        )
        return msg.request_id
    finally:
        await redis.close()
