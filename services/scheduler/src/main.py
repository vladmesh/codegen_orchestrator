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

import structlog

from shared.contracts.queues.provisioner import ProvisionerResult
from shared.log_config import setup_logging
from shared.queues import PROVISIONER_RESULTS, SCHEDULER_CONSUMER_GROUP
from shared.redis_client import RedisStreamClient

from .tasks.github_sync import sync_projects_worker
from .tasks.health_checker import health_check_worker
from .tasks.provisioner_result_listener import process_provisioner_result
from .tasks.provisioner_trigger import retry_pending_servers
from .tasks.rag_summarizer import rag_summarizer_worker
from .tasks.server_sync import sync_servers_worker
from .tasks.task_dispatcher import task_dispatcher_loop

logger = structlog.get_logger()

CONSUMER_NAME = f"scheduler-{os.getpid()}"


async def provisioner_results_worker():
    """Consumer loop for provisioner:results stream.

    Listens for ProvisionerResult messages from infra-service
    and updates server status via API.
    """
    client = RedisStreamClient()
    await client.connect()

    logger.info(
        "provisioner_results_worker_started",
        stream=PROVISIONER_RESULTS,
        consumer=CONSUMER_NAME,
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
                result = ProvisionerResult.model_validate(msg.data)
                await process_provisioner_result(result)
                await client.ack(PROVISIONER_RESULTS, SCHEDULER_CONSUMER_GROUP, msg.message_id)
            except Exception as e:
                logger.error(
                    "provisioner_result_processing_error",
                    entry_id=msg.message_id,
                    error=str(e),
                    exc_info=True,
                )
    finally:
        await client.close()
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
            "task_dispatcher",
        ],
    )

    # Create tasks for all workers
    tasks = [
        asyncio.create_task(sync_servers_worker(), name="server_sync"),
        asyncio.create_task(sync_projects_worker(), name="github_sync"),
        asyncio.create_task(health_check_worker(), name="health_checker"),
        asyncio.create_task(rag_summarizer_worker(), name="rag_summarizer"),
        asyncio.create_task(provisioner_results_worker(), name="provisioner_results"),
        asyncio.create_task(task_dispatcher_loop(), name="task_dispatcher"),
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
