"""Small lifecycle helpers shared by the scheduler service processes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping
from pathlib import Path

import structlog

from shared.config_store import ConfigStoreUnavailableError
from shared.log_config import unbind_message_context
from shared.redis import RedisStreamClient

from . import startup

logger = structlog.get_logger()
CONFIG_VALIDATION_RETRY_SECONDS = 2
READINESS_PATH = Path("/tmp/scheduler-service-ready")  # noqa: S108 - container-local signal


async def initialize_configs(required_keys: Collection[str], *, service_name: str) -> None:
    """Wait for the config API, then validate this process's owned keys."""
    READINESS_PATH.unlink(missing_ok=True)
    while True:
        try:
            await startup.init_config(required_keys)
            logger.info("system_configs_validated", service=service_name)
            return
        except ConfigStoreUnavailableError as exc:
            logger.warning(
                "system_config_api_unavailable_retrying",
                service=service_name,
                error=str(exc),
                retry_in_seconds=CONFIG_VALIDATION_RETRY_SECONDS,
            )
            await asyncio.sleep(CONFIG_VALIDATION_RETRY_SECONDS)


async def periodic_loop(
    *,
    interval: Callable[[], float],
    cycle: Callable[[RedisStreamClient], Awaitable[Mapping[str, object] | None]],
    logger: structlog.stdlib.BoundLogger,
    started_event: str,
    cycle_event: str | None,
    error_event: str,
    stopped_event: str,
    redis_factory: Callable[[], RedisStreamClient] = RedisStreamClient,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    wake: asyncio.Event | None = None,
) -> None:
    """Run one Redis-backed periodic worker with a stable lifecycle.

    Each cycle starts with the message context unbound, so its messages never inherit
    a correlation id bound before it, at startup or by the previous cycle. With `wake`,
    the wait after a cycle also ends when the event is set; a set during a cycle or
    before the wait still runs one prompt follow-up cycle.
    """
    redis_client = redis_factory()
    await redis_client.connect()
    logger.info(started_event, interval=interval())
    try:
        while True:
            unbind_message_context()
            if wake is not None:
                wake.clear()
            try:
                fields = await cycle(redis_client)
                if cycle_event is not None:
                    logger.info(cycle_event, **dict(fields or {}))
            except Exception:
                logger.exception(error_event)
            if wake is None:
                await sleep(interval())
            else:
                await _wait_for_wake(wake, sleep(interval()))
    finally:
        await redis_client.close()
        logger.info(stopped_event)


async def _wait_for_wake(wake: asyncio.Event, timer: Awaitable[None]) -> None:
    """Return when `wake` is set or `timer` finishes, cancelling and awaiting the other."""
    waiters = {asyncio.ensure_future(wake.wait()), asyncio.ensure_future(timer)}
    try:
        done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
    for waiter in done:
        waiter.result()


async def run_workers(
    workers: Collection[tuple[str, Callable[[], Awaitable[None]]]],
    *,
    service_name: str,
) -> None:
    """Run one process's loops beside its config refresher; close every sibling when one exits."""
    tasks = [
        asyncio.create_task(startup.get_config().refresh_forever(), name="config_refresh"),
        *(asyncio.create_task(worker(), name=name) for name, worker in workers),
    ]
    try:
        logger.info(
            "service_workers_started", service=service_name, workers=[t.get_name() for t in tasks]
        )
        READINESS_PATH.write_text(service_name)
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        completed = next(iter(done))
        exception = completed.exception()
        if exception is not None:
            raise exception
        raise RuntimeError(f"long-lived worker exited: {completed.get_name()}")
    except asyncio.CancelledError:
        logger.info("service_shutdown_requested", service=service_name)
    except Exception:
        logger.exception("service_worker_failed", service=service_name)
        raise
    finally:
        READINESS_PATH.unlink(missing_ok=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("service_workers_stopped", service=service_name)
