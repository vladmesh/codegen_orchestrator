"""Pipeline scheduler service entry point."""

import asyncio

import structlog

from shared.log_config import setup_logging

from . import runtime
from .startup import PIPELINE_REQUIRED_KEYS
from .tasks.owner_notification_loop import owner_notification_loop
from .tasks.task_dispatcher import task_dispatcher_loop
from .tasks.temporary_access_loop import temporary_access_loop
from .tasks.worker_reconciliation import worker_reconciliation_loop

logger = structlog.get_logger()


async def main() -> None:
    setup_logging(service_name="scheduler-pipeline")
    logger.info("scheduler_pipeline_started")
    await runtime.initialize_configs(PIPELINE_REQUIRED_KEYS, service_name="scheduler-pipeline")
    await runtime.run_workers(
        [
            ("task_dispatcher", task_dispatcher_loop),
            ("worker_reconciliation", worker_reconciliation_loop),
            ("temporary_access", temporary_access_loop),
            ("owner_notifications", owner_notification_loop),
        ],
        service_name="scheduler-pipeline",
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("scheduler_pipeline_stopped_by_user")
