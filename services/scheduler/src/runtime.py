"""Small lifecycle helpers shared by the scheduler service processes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path

import structlog

from shared.config_store import ConfigStoreUnavailableError

from . import startup

logger = structlog.get_logger()
CONFIG_VALIDATION_RETRY_SECONDS = 2
READINESS_PATH = Path("/tmp/scheduler-service-ready")  # noqa: S108 - container-local signal


async def initialize_configs(required_keys: Collection[str], *, service_name: str) -> None:
    """Wait for the config API, then validate this process's owned keys."""
    READINESS_PATH.unlink(missing_ok=True)
    while True:
        try:
            startup.init_config(required_keys)
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


async def run_workers(
    workers: Collection[tuple[str, Callable[[], Awaitable[None]]]],
    *,
    service_name: str,
) -> None:
    """Run one process's loops and close every sibling when one exits."""
    tasks = [asyncio.create_task(worker(), name=name) for name, worker in workers]
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
