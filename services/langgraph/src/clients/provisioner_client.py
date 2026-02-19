"""Client for infrastructure-worker via Redis queue.

Fire-and-forget: queues provisioning jobs to provisioner:queue.
Results are handled by infra-service → scheduler pipeline.
"""

from __future__ import annotations

import json
import uuid

import structlog

from shared.redis_client import RedisStreamClient

logger = structlog.get_logger(__name__)

PROVISIONER_QUEUE = "provisioner:queue"


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
    request_id = str(uuid.uuid4())

    job_data = {
        "request_id": request_id,
        "server_handle": server_handle,
        "force_reinstall": force_reinstall,
        "is_recovery": is_recovery,
        "correlation_id": correlation_id,
    }

    redis = RedisStreamClient()
    await redis.connect()

    try:
        await redis.redis.xadd(PROVISIONER_QUEUE, {"data": json.dumps(job_data)})
        logger.info(
            "provisioning_job_queued",
            request_id=request_id,
            server_handle=server_handle,
        )
        return request_id
    finally:
        await redis.close()
