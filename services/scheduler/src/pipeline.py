"""Pipeline scheduler service entry point."""

import asyncio

import structlog

from shared.log_config import setup_logging

from . import runtime
from .startup import PIPELINE_REQUIRED_KEYS
from .tasks.task_dispatcher import task_dispatcher_loop

logger = structlog.get_logger()


async def main() -> None:
    setup_logging(service_name="scheduler-pipeline")
    logger.info("scheduler_pipeline_started")
    await runtime.initialize_configs(PIPELINE_REQUIRED_KEYS, service_name="scheduler-pipeline")
    await runtime.run_workers(
        [("task_dispatcher", task_dispatcher_loop)], service_name="scheduler-pipeline"
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("scheduler_pipeline_stopped_by_user")
