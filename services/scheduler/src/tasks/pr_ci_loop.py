"""Independent scheduler loop for pull-request and CI-result routing."""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import runtime, startup
from .pr_poller import poll_ci_failures, poll_merged_prs

logger = structlog.get_logger(__name__)


def _pr_ci_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def poll_pr_ci_once(api_client, redis_client: RedisStreamClient) -> dict[str, int]:
    merged = 0
    ci_failures = 0
    try:
        merged = await poll_merged_prs(api_client, redis_client)
    except Exception:
        logger.exception("pr_merge_poll_error")
    try:
        ci_failures = await poll_ci_failures(api_client, redis_client)
    except Exception:
        logger.exception("ci_failure_poll_error")
    return {"prs_merged": merged, "ci_failures_routed": ci_failures}


async def pr_ci_loop() -> None:
    from ..clients.api import api_client
    async def cycle(redis_client: RedisStreamClient) -> dict[str, object]:
        return await poll_pr_ci_once(api_client, redis_client)
    await runtime.periodic_loop(
        interval=_pr_ci_interval, cycle=cycle, logger=logger,
        started_event="pr_ci_started", cycle_event="pr_ci_cycle",
        error_event="pr_ci_cycle_error", stopped_event="pr_ci_stopped",
        redis_factory=RedisStreamClient, sleep=asyncio.sleep,
    )
