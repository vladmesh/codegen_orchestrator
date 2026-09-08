"""Application lifespan events."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from codegen_kit.packages import shutdown_packages, startup_packages
from shared.generated.events import get_broker

from ..core.logging import configure_logging


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan context manager."""
    configure_logging()
    broker = get_broker()
    await broker.connect()
    active_error: BaseException | None = None
    try:
        await startup_packages(application)
        yield
    except BaseException as error:
        active_error = error
        raise
    finally:
        try:
            await shutdown_packages(application, suppress_errors=active_error is not None)
        finally:
            await broker.close()
