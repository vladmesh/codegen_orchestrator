"""Independent scheduler loop for pull-request and CI-result routing."""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import startup
from .pr_poller import poll_ci_failures, poll_merged_prs

logger = structlog.get_logger(__name__)


def _pr_ci_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def pr_ci_loop() -> None:
    """Poll merged PRs and CI failures outside the ordered dispatcher tick.

    Both routes read durable pr_review and GitHub facts. They share a cadence
    and Redis connection, but not a failure boundary: a GitHub/API failure in one
    route must not delay the other until the next scheduler cycle.
    """
    from ..clients.api import api_client

    redis_client = RedisStreamClient()
    await redis_client.connect()
    logger.info("pr_ci_started", interval=_pr_ci_interval())

    try:
        while True:
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
            logger.info(
                "pr_ci_cycle",
                prs_merged=merged,
                ci_failures_routed=ci_failures,
            )
            await asyncio.sleep(_pr_ci_interval())
    finally:
        await redis_client.close()
        logger.info("pr_ci_stopped")
