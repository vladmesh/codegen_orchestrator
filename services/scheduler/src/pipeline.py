"""Pipeline scheduler service entry point."""

import asyncio
from functools import partial

import structlog

from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.log_config import setup_logging

from . import runtime
from .startup import PIPELINE_REQUIRED_KEYS
from .tasks.lifecycle_supervision_loop import lifecycle_supervision_loop
from .tasks.owner_notification_loop import owner_notification_loop
from .tasks.pr_ci_loop import pr_ci_loop
from .tasks.qa_routing_loop import qa_routing_loop
from .tasks.scaffold_loop import scaffold_loop
from .tasks.scheduler_wake_listener import scheduler_wake_listener
from .tasks.story_completion_loop import story_completion_loop
from .tasks.story_supervision_loop import story_supervision_loop
from .tasks.task_dispatcher import task_dispatcher_loop
from .tasks.temporary_access_loop import temporary_access_loop
from .tasks.worker_reconciliation import worker_reconciliation_loop

logger = structlog.get_logger()


async def main() -> None:
    setup_logging(service_name="scheduler-pipeline")
    logger.info("scheduler_pipeline_started")
    await runtime.initialize_configs(PIPELINE_REQUIRED_KEYS, service_name="scheduler-pipeline")
    task_dispatcher_wake = asyncio.Event()
    story_completion_wake = asyncio.Event()
    lifecycle_supervision_wake = asyncio.Event()
    temporary_access_wake = asyncio.Event()
    qa_routing_wake = asyncio.Event()
    pr_ci_wake = asyncio.Event()
    await runtime.run_workers(
        [
            ("task_dispatcher", partial(task_dispatcher_loop, wake=task_dispatcher_wake)),
            ("scaffold", scaffold_loop),
            ("story_completion", partial(story_completion_loop, wake=story_completion_wake)),
            (
                "scheduler_wake",
                partial(
                    scheduler_wake_listener,
                    {
                        SchedulerWakeTarget.TASK_DISPATCHER: task_dispatcher_wake,
                        SchedulerWakeTarget.STORY_COMPLETION: story_completion_wake,
                        SchedulerWakeTarget.LIFECYCLE_SUPERVISION: lifecycle_supervision_wake,
                        SchedulerWakeTarget.TEMPORARY_ACCESS: temporary_access_wake,
                        SchedulerWakeTarget.QA_ROUTING: qa_routing_wake,
                        SchedulerWakeTarget.PR_CI: pr_ci_wake,
                    },
                ),
            ),
            (
                "lifecycle_supervision",
                partial(lifecycle_supervision_loop, wake=lifecycle_supervision_wake),
            ),
            ("qa_routing", partial(qa_routing_loop, wake=qa_routing_wake)),
            ("pr_ci", partial(pr_ci_loop, wake=pr_ci_wake)),
            ("worker_reconciliation", worker_reconciliation_loop),
            ("temporary_access", partial(temporary_access_loop, wake=temporary_access_wake)),
            ("owner_notifications", owner_notification_loop),
            ("story_supervision", story_supervision_loop),
        ],
        service_name="scheduler-pipeline",
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("scheduler_pipeline_stopped_by_user")
