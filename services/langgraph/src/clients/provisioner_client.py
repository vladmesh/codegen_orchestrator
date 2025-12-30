"""Client for infrastructure-worker via Redis queue.

Used to trigger provisioning jobs and wait for results.
"""

from __future__ import annotations

import json
import uuid

import structlog

from shared.redis_client import RedisStreamClient

logger = structlog.get_logger(__name__)

PROVISIONER_QUEUE = "provisioner:queue"
PROVISIONER_RESULT_PREFIX = "provisioner:result"


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
        Request ID to poll for results
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


async def wait_for_result(request_id: str, timeout: int = 1200) -> dict | None:
    """Wait for provisioning result.

    Args:
        request_id: Request ID from trigger_provisioning
        timeout: Timeout in seconds (default 20 minutes)

    Returns:
        Result dict or None if timeout
    """
    result_key = f"{PROVISIONER_RESULT_PREFIX}:{request_id}"

    redis = RedisStreamClient()
    await redis.connect()

    import asyncio

    try:
        poll_interval = 5  # seconds
        elapsed = 0

        while elapsed < timeout:
            result = await redis.redis.get(result_key)
            if result:
                return json.loads(result)

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        logger.warning(
            "provisioning_result_timeout",
            request_id=request_id,
            timeout=timeout,
        )
        return None
    finally:
        await redis.close()


async def get_result(request_id: str) -> dict | None:
    """Get provisioning result without waiting.

    Args:
        request_id: Request ID from trigger_provisioning

    Returns:
        Result dict or None if not yet available
    """
    result_key = f"{PROVISIONER_RESULT_PREFIX}:{request_id}"

    redis = RedisStreamClient()
    await redis.connect()

    try:
        result = await redis.redis.get(result_key)
        if result:
            return json.loads(result)
        return None
    finally:
        await redis.close()
