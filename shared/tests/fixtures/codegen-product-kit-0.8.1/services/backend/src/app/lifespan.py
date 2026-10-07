"""Application lifespan events."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from codegen_kit.packages import shutdown_packages, startup_packages
from shared.generated.events import get_broker

from ..core.logging import configure_logging
from ..generated.jobs_schemas import JOB_TIMERS
from .timers import start_timer_loop


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan context manager."""
    configure_logging()
    broker = get_broker()
    await broker.connect()
    application.state.codegen_timer_loop = None
    active_error: BaseException | None = None
    try:
        await startup_packages(application)
        # The one core timer loop starts after every package consumer is ready and is
        # stopped before any of them stops; a product without timers starts none.
        application.state.codegen_timer_loop = start_timer_loop(JOB_TIMERS)
        yield
    except BaseException as error:
        active_error = error
        raise
    finally:
        try:
            timer_loop = application.state.codegen_timer_loop
            if timer_loop is not None:
                await timer_loop.stop()
        finally:
            try:
                await shutdown_packages(application, suppress_errors=active_error is not None)
            finally:
                await broker.close()
