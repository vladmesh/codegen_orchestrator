"""Independent worker teardown reconciliation for the scheduler pipeline.

The dispatcher owns order-sensitive product routing. Worker teardown does not:
both reconciliation passes act only on durable terminal facts, so they can run
on their own clock/failure boundary without making dispatcher call order part
of their correctness contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog

from shared.redis import RedisStreamClient

from .gave_up_worker_reconciliation import reconcile_gave_up_attempt_workers
from .terminal_worker_reconciliation import reconcile_terminal_story_workers

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

from .. import runtime, startup

logger = structlog.get_logger(__name__)

_Reconciler = Callable[..., Awaitable[int]]


def _reconciliation_interval() -> int:
    """Use the pipeline cadence without coupling teardown to dispatcher execution."""
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def _run_reconciler(
    name: str,
    reconciler: _Reconciler,
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> int:
    """Run one durable scan without letting its failure suppress the sibling scan."""
    try:
        return await reconciler(api_client, redis_client)
    except Exception:
        logger.exception("worker_reconciliation_scan_error", reconciliation=name)
        return 0


async def reconcile_workers_once(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Run both teardown scans once and return their requested teardown counts."""
    terminal_workers = await _run_reconciler(
        "terminal_story_workers",
        reconcile_terminal_story_workers,
        api_client,
        redis_client,
    )
    gave_up_workers = await _run_reconciler(
        "gave_up_attempt_workers",
        reconcile_gave_up_attempt_workers,
        api_client,
        redis_client,
    )
    counts = {
        "terminal_workers_requested": terminal_workers,
        "gave_up_workers_requested": gave_up_workers,
    }
    logger.info("worker_reconciliation_cycle", **counts)
    return counts


async def worker_reconciliation_loop() -> None:
    """Periodically reconcile workers from durable terminal/settled-attempt facts."""
    from ..clients.api import api_client

    async def cycle(redis_client: RedisStreamClient) -> None:
        await reconcile_workers_once(api_client, redis_client)

    await runtime.periodic_loop(
        interval=_reconciliation_interval,
        cycle=cycle,
        logger=logger,
        started_event="worker_reconciliation_started",
        cycle_event=None,
        error_event="worker_reconciliation_cycle_error",
        stopped_event="worker_reconciliation_stopped",
        redis_factory=RedisStreamClient,
        sleep=asyncio.sleep,
    )
