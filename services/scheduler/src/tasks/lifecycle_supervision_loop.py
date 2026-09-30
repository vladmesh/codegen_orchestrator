"""Independent scheduler loop for pipeline lifecycle supervision."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog

from shared.redis import RedisStreamClient

from .. import startup
from .supervisor import (
    supervise_deploying_stories,
    supervise_failed_tasks,
    supervise_stuck_stories,
    supervise_stuck_tasks,
    supervise_waiting_resource_tasks,
    supervise_waiting_user_secret_stories,
)

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)

_Sweep = Callable[
    ["SchedulerAPIClient", RedisStreamClient],
    Awaitable[dict[str, int]],
]


def _lifecycle_supervision_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def _run_sweep(
    name: str,
    sweep: _Sweep,
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Run one durable supervisor without letting it suppress sibling sweeps."""
    try:
        return await sweep(api_client, redis_client)
    except Exception:
        logger.exception("lifecycle_supervision_sweep_error", sweep=name)
        return {}


async def supervise_lifecycle_once(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> dict[str, int]:
    """Run lifecycle supervisors in the historical order with isolated failures."""
    sweeps: tuple[tuple[str, _Sweep], ...] = (
        ("stuck_stories", supervise_stuck_stories),
        ("stuck_tasks", supervise_stuck_tasks),
        ("failed_tasks", supervise_failed_tasks),
        ("waiting_resources", supervise_waiting_resource_tasks),
        ("deploying", supervise_deploying_stories),
        ("waiting_user_secret", supervise_waiting_user_secret_stories),
    )
    counts: dict[str, int] = {}
    for name, sweep in sweeps:
        result = await _run_sweep(name, sweep, api_client, redis_client)
        counts.update({f"{name}_{metric}": value for metric, value in result.items()})
    logger.info("lifecycle_supervision_cycle", **counts)
    return counts


async def lifecycle_supervision_loop() -> None:
    """Supervise lifecycle state on its own cadence and Redis lifecycle."""
    from ..clients.api import api_client

    redis_client = RedisStreamClient()
    await redis_client.connect()
    logger.info(
        "lifecycle_supervision_started",
        interval=_lifecycle_supervision_interval(),
    )

    try:
        while True:
            try:
                await supervise_lifecycle_once(api_client, redis_client)
            except Exception:
                logger.exception("lifecycle_supervision_cycle_error")
            await asyncio.sleep(_lifecycle_supervision_interval())
    finally:
        await redis_client.close()
        logger.info("lifecycle_supervision_stopped")
