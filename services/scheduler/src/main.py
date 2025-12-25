"""Scheduler service entry point.

Runs all background workers:
- GitHub Sync: Syncs projects from GitHub organization
- Server Sync: Syncs servers from Time4VPS provider
- Health Checker: Monitors server health via SSH
- Provisioner Trigger: Listens for provisioning requests
"""

import asyncio
import logging
import sys

from .tasks.github_sync import sync_projects_worker
from .tasks.health_checker import health_check_worker
from .tasks.provisioner_trigger import provisioner_trigger_worker
from .tasks.server_sync import sync_servers_worker

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)


async def main():
    """Run all background workers concurrently."""
    logger.info("🚀 Starting Scheduler Service")
    logger.info("Workers: github_sync, server_sync, health_checker, provisioner_trigger")

    # Create tasks for all workers
    tasks = [
        asyncio.create_task(sync_servers_worker(), name="server_sync"),
        asyncio.create_task(sync_projects_worker(), name="github_sync"),
        asyncio.create_task(health_check_worker(), name="health_checker"),
        asyncio.create_task(provisioner_trigger_worker(), name="provisioner_trigger"),
    ]

    try:
        # Wait for all tasks (they run forever)
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Scheduler shutdown requested")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.error(f"Scheduler error: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user")
