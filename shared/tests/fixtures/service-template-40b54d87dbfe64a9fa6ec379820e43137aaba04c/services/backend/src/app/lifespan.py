"""Application lifespan events."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from shared.generated.events import get_broker

from ..core.logging import configure_logging


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan context manager."""
    configure_logging()

    broker = get_broker()
    await broker.connect()

    yield

    await broker.close()
