"""Scheduler service entry point.

Runs all background workers:
- GitHub Sync: Syncs projects from GitHub organization
- Server Sync: Syncs servers from Time4VPS provider
- Health Checker: Monitors server health via SSH
- RAG Summarizer: Summarizes project documentation
- Provisioner Result Listener: Handles provisioning results from infra-service
"""

import asyncio
import os

import redis.asyncio as redis
import structlog

from shared.contracts.queues.provisioner import ProvisionerResult
from shared.logging_config import setup_logging

from .tasks.github_sync import sync_projects_worker
from .tasks.health_checker import health_check_worker
from .tasks.provisioner_result_listener import process_provisioner_result
from .tasks.provisioner_trigger import retry_pending_servers
from .tasks.rag_summarizer import rag_summarizer_worker
from .tasks.server_sync import sync_servers_worker

logger = structlog.get_logger()

# Redis configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PROVISIONER_RESULTS_STREAM = "provisioner:results"
PROVISIONER_RESULTS_GROUP = "scheduler-consumers"
CONSUMER_NAME = f"scheduler-{os.getpid()}"


async def provisioner_results_worker():
    """Consumer loop for provisioner:results stream.

    Listens for ProvisionerResult messages from infra-service
    and updates server status via API.
    """
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)

    try:
        # Create consumer group if doesn't exist
        try:
            await redis_client.xgroup_create(
                PROVISIONER_RESULTS_STREAM,
                PROVISIONER_RESULTS_GROUP,
                id="0",
                mkstream=True,
            )
            logger.info(
                "consumer_group_created",
                stream=PROVISIONER_RESULTS_STREAM,
                group=PROVISIONER_RESULTS_GROUP,
            )
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.debug("consumer_group_exists", group=PROVISIONER_RESULTS_GROUP)
            else:
                raise

        logger.info(
            "provisioner_results_worker_started",
            stream=PROVISIONER_RESULTS_STREAM,
            consumer=CONSUMER_NAME,
        )

        while True:
            try:
                # Read from stream with blocking
                messages = await redis_client.xreadgroup(
                    groupname=PROVISIONER_RESULTS_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={PROVISIONER_RESULTS_STREAM: ">"},
                    count=1,
                    block=5000,  # 5 second block
                )

                if not messages:
                    continue

                for _stream_name, entries in messages:
                    for entry_id, raw_data in entries:
                        try:
                            # Parse ProvisionerResult from JSON
                            if "data" in raw_data:
                                result = ProvisionerResult.model_validate_json(raw_data["data"])
                            else:
                                logger.warning(
                                    "invalid_message_format",
                                    entry_id=entry_id,
                                    keys=list(raw_data.keys()),
                                )
                                continue

                            # Process the result
                            await process_provisioner_result(result)

                            # ACK the message
                            await redis_client.xack(
                                PROVISIONER_RESULTS_STREAM,
                                PROVISIONER_RESULTS_GROUP,
                                entry_id,
                            )
                            logger.debug("message_acked", entry_id=entry_id)

                        except Exception as e:
                            logger.error(
                                "provisioner_result_processing_error",
                                entry_id=entry_id,
                                error=str(e),
                                exc_info=True,
                            )
                            # Don't ACK - message will be redelivered

            except asyncio.CancelledError:
                logger.info("provisioner_results_worker_cancelled")
                break
            except Exception as e:
                logger.error(
                    "provisioner_results_worker_error",
                    error=str(e),
                    exc_info=True,
                )
                await asyncio.sleep(1)  # Backoff on error

    finally:
        await redis_client.aclose()
        logger.info("provisioner_results_worker_stopped")


async def main():
    """Run all background workers concurrently."""
    setup_logging(service_name="scheduler")
    logger.info("scheduler_started")

    # Retry provisioning for any servers stuck in pending_setup (race condition fix)
    # Wait a bit for LangGraph to be ready and subscribed
    await asyncio.sleep(5)
    await retry_pending_servers()

    logger.info(
        "scheduler_workers_configured",
        workers=[
            "github_sync",
            "server_sync",
            "health_checker",
            "rag_summarizer",
            "provisioner_results",
        ],
    )

    # Create tasks for all workers
    tasks = [
        asyncio.create_task(sync_servers_worker(), name="server_sync"),
        asyncio.create_task(sync_projects_worker(), name="github_sync"),
        asyncio.create_task(health_check_worker(), name="health_checker"),
        asyncio.create_task(rag_summarizer_worker(), name="rag_summarizer"),
        asyncio.create_task(provisioner_results_worker(), name="provisioner_results"),
    ]

    try:
        # Wait for all tasks (they run forever)
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("scheduler_shutdown_requested")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.error(
            "scheduler_error",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("scheduler_stopped_by_user")
