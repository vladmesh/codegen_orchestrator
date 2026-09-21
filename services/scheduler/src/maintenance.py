"""Maintenance scheduler service entry point."""

import asyncio

import structlog

from shared.log_config import setup_logging

from . import runtime
from .startup import MAINTENANCE_REQUIRED_KEYS
from .tasks.analytics_aggregator import analytics_aggregator_worker
from .tasks.github_sync import sync_projects_worker
from .tasks.queue_cleanup import queue_cleanup_worker
from .tasks.rag_summarizer import rag_summarizer_worker

logger = structlog.get_logger()


async def main() -> None:
    setup_logging(service_name="scheduler-maintenance")
    logger.info("scheduler_maintenance_started")
    await runtime.initialize_configs(
        MAINTENANCE_REQUIRED_KEYS, service_name="scheduler-maintenance"
    )
    await runtime.run_workers(
        [
            ("github_sync", sync_projects_worker),
            ("rag_summarizer", rag_summarizer_worker),
            ("analytics_aggregator", analytics_aggregator_worker),
            ("queue_cleanup", queue_cleanup_worker),
        ],
        service_name="scheduler-maintenance",
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("scheduler_maintenance_stopped_by_user")
