"""Infrastructure Worker — consumes from provisioner:queue and runs provisioning.

Run standalone: python -m src.main
"""

from __future__ import annotations

import asyncio
import json
import os
import signal

import structlog

from shared.logging_config import setup_logging
from shared.redis_client import RedisStreamClient

from .provisioner.node import ProvisionerNode

logger = structlog.get_logger(__name__)

# Queue configuration
PROVISIONER_QUEUE = "provisioner:queue"
PROVISIONER_GROUP = "infrastructure-workers"
CONSUMER_NAME = f"infra-worker-{os.getpid()}"

# Shutdown flag
_shutdown = False


def handle_shutdown(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown
    logger.info("shutdown_signal_received", signal=signum)
    _shutdown = True


async def ensure_consumer_group(redis_client) -> None:
    """Ensure Redis consumer group exists."""
    try:
        await redis_client.xgroup_create(
            PROVISIONER_QUEUE, PROVISIONER_GROUP, id="0", mkstream=True
        )
        logger.info("consumer_group_created", group=PROVISIONER_GROUP)
    except Exception as e:
        # Group already exists
        if "BUSYGROUP" in str(e):
            logger.debug("consumer_group_exists", group=PROVISIONER_GROUP)
        else:
            raise


async def process_provisioner_job(job_data: dict) -> dict:
    """Process a single provisioner job.

    Args:
        job_data: Job data from Redis queue

    Returns:
        Result dict with status and details
    """
    job_id = job_data.get("job_id") or job_data.get("request_id", "unknown")
    server_handle = job_data.get("server_handle")

    logger.info(
        "provisioner_job_started",
        job_id=job_id,
        server_handle=server_handle,
    )

    try:
        # Build state for ProvisionerNode
        state = {
            "server_to_provision": server_handle,
            "is_incident_recovery": job_data.get("is_recovery", False),
            "force_reinstall": job_data.get("force_reinstall", False),
            "errors": [],
        }

        # Run provisioner
        node = ProvisionerNode()
        result = await node.run(state)

        # Extract result
        provisioning_result = result.get("provisioning_result", {})
        status = provisioning_result.get("status", "unknown")

        if status == "success":
            logger.info(
                "provisioner_job_success",
                job_id=job_id,
                server_handle=server_handle,
                server_ip=provisioning_result.get("server_ip"),
            )
            return {
                "status": "success",
                "server_handle": server_handle,
                "server_ip": provisioning_result.get("server_ip"),
                "services_redeployed": provisioning_result.get("services_redeployed", 0),
            }
        else:
            errors = result.get("errors", ["Unknown error"])
            logger.error(
                "provisioner_job_failed",
                job_id=job_id,
                server_handle=server_handle,
                errors=errors,
            )
            return {
                "status": "failed",
                "server_handle": server_handle,
                "errors": errors,
            }

    except Exception as e:
        logger.error(
            "provisioner_job_exception",
            job_id=job_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return {
            "status": "failed",
            "server_handle": server_handle,
            "error": str(e),
        }


async def run_worker():
    """Main worker loop."""
    setup_logging(service_name="infrastructure-worker")

    redis = RedisStreamClient()
    await redis.connect()

    # Ensure consumer group exists
    await ensure_consumer_group(redis.redis)

    logger.info("infrastructure_worker_started", consumer=CONSUMER_NAME)

    try:
        while not _shutdown:
            try:
                # Read from consumer group
                messages = await redis.redis.xreadgroup(
                    groupname=PROVISIONER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={PROVISIONER_QUEUE: ">"},
                    count=1,
                    block=5000,  # 5 second block
                )

                if not messages:
                    continue

                for _stream_name, entries in messages:
                    for entry_id, raw_data in entries:
                        try:
                            # Parse JSON data
                            if "data" in raw_data:
                                job_data = json.loads(raw_data["data"])
                            else:
                                job_data = raw_data

                            # Process the job
                            result = await process_provisioner_job(job_data)

                            # Publish result if request_id provided
                            request_id = job_data.get("request_id")
                            if request_id:
                                result_key = f"provisioner:result:{request_id}"
                                await redis.redis.set(
                                    result_key,
                                    json.dumps(result),
                                    ex=3600,  # 1 hour TTL
                                )

                            # ACK the message
                            await redis.redis.xack(PROVISIONER_QUEUE, PROVISIONER_GROUP, entry_id)
                            logger.debug("provisioner_job_acked", entry_id=entry_id)

                        except Exception as e:
                            logger.error(
                                "provisioner_job_processing_error",
                                entry_id=entry_id,
                                error=str(e),
                            )
                            # Don't ACK — job will be redelivered

            except asyncio.CancelledError:
                logger.info("worker_cancelled")
                break
            except Exception as e:
                logger.error("worker_loop_error", error=str(e))
                await asyncio.sleep(1)

    finally:
        await redis.close()
        logger.info("infrastructure_worker_shutdown")


def main():
    """Entry point for running as module."""
    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
