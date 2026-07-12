"""Infrastructure Worker — consumes from provisioner:queue.

Run standalone: python -m src.main
"""

from __future__ import annotations

import asyncio
import os
import signal

import structlog

from shared.contracts.queues.provisioner import ProvisionerMessage, ProvisionerResult
from shared.contracts.vocab import ResultStatus
from shared.log_config import setup_logging
from shared.queues import INFRA_GROUP, PROVISIONER_QUEUE
from shared.redis_client import RedisStreamClient

from .provisioner.node import ProvisionerNode

logger = structlog.get_logger(__name__)

# Consumer configuration
CONSUMER_NAME = f"infra-worker-{os.getpid()}"

# Shutdown flag
_shutdown = False


def handle_shutdown(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown
    logger.info("shutdown_signal_received", signal=signum)
    _shutdown = True


async def process_provisioner_job(job_data: dict) -> ProvisionerResult:
    """Process a single provisioner job.

    Args:
        job_data: Job data from Redis queue

    Returns:
        ProvisionerResult with status and details
    """
    job_id = job_data.get("job_id") or job_data.get("request_id", "unknown")
    server_handle = job_data.get("server_handle", "")

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
            return ProvisionerResult(
                request_id=job_id,
                status=ResultStatus.SUCCESS,
                server_handle=server_handle,
                server_ip=provisioning_result.get("server_ip"),
                services_redeployed=provisioning_result.get("services_redeployed", 0),
            )
        else:
            errors = result.get("errors", ["Unknown error"])
            logger.error(
                "provisioner_job_failed",
                job_id=job_id,
                server_handle=server_handle,
                errors=errors,
            )
            return ProvisionerResult(
                request_id=job_id,
                status=ResultStatus.FAILED,
                server_handle=server_handle,
                errors=errors,
            )

    except Exception as e:
        logger.error(
            "provisioner_job_exception",
            job_id=job_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return ProvisionerResult(
            request_id=job_id,
            status=ResultStatus.FAILED,
            server_handle=server_handle,
            error=str(e),
        )


async def run_worker():
    """Main worker loop handling provisioning queue."""
    setup_logging(service_name="infra-service")

    client = RedisStreamClient()
    await client.connect()

    logger.info("infrastructure_worker_started", consumer=CONSUMER_NAME)

    try:
        async for msg in client.consume(
            PROVISIONER_QUEUE,
            INFRA_GROUP,
            CONSUMER_NAME,
            auto_ack=False,
            claim_pending=True,
        ):
            if _shutdown:
                break
            if msg is None:
                continue
            try:
                job = ProvisionerMessage.model_validate(msg.data)
                result = await process_provisioner_job(job.model_dump(mode="json"))

                # Publish result
                result_key = f"deploy:result:{result.request_id}"
                await client.redis.set(result_key, result.model_dump_json(), ex=3600)
                await client.publish("provisioner:results", result.model_dump(mode="json"))

                await client.ack(PROVISIONER_QUEUE, INFRA_GROUP, msg.message_id)
                logger.debug("job_acked", entry_id=msg.message_id)

            except Exception as e:
                logger.error(
                    "job_processing_error",
                    entry_id=msg.message_id,
                    error=str(e),
                    exc_info=True,
                )
    finally:
        await client.close()
        logger.info("infrastructure_worker_shutdown")


def main():
    """Entry point for running as module."""
    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
