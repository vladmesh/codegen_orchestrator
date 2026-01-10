"""Scheduler service entry point.

Runs all background workers:
- GitHub Sync: Syncs projects from GitHub organization
- Server Sync: Syncs servers from Time4VPS provider
- Health Checker: Monitors server health via SSH
- RAG Summarizer: Summarizes project documentation
"""

import asyncio

import structlog

from shared.logging_config import setup_logging

from .tasks.github_sync import sync_projects_worker
from .tasks.health_checker import health_check_worker
from .tasks.provisioner_trigger import retry_pending_servers
from .tasks.rag_summarizer import rag_summarizer_worker
from .tasks.server_sync import sync_servers_worker

logger = structlog.get_logger()


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
        ],
    )

    # Create tasks for all workers
    # Note: provisioner_trigger_worker removed - LangGraph handles this now
    tasks = [
        asyncio.create_task(sync_servers_worker(), name="server_sync"),
        asyncio.create_task(sync_projects_worker(), name="github_sync"),
        asyncio.create_task(health_check_worker(), name="health_checker"),
        asyncio.create_task(rag_summarizer_worker(), name="rag_summarizer"),
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
