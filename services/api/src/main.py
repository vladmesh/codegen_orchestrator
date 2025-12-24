"""API Service - FastAPI with SQLAlchemy."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import routers
from .database import engine
from .tasks.server_sync import sync_servers_worker
from .tasks.health_checker import health_check_worker


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    # Startup
    asyncio.create_task(sync_servers_worker())
    asyncio.create_task(health_check_worker())
    yield
    # Shutdown
    await engine.dispose()


app = FastAPI(
    title="Codegen Orchestrator API",
    description="Internal API for database access",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(routers.health.router)
app.include_router(routers.resources.router)
app.include_router(routers.users.router)
app.include_router(routers.projects.router)
app.include_router(routers.servers.router, prefix="/api")
app.include_router(routers.api_keys.router, prefix="/api")
app.include_router(routers.incidents.router, prefix="/api")
