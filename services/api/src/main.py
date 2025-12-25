"""API Service - FastAPI with SQLAlchemy."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import routers
from .database import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    # Startup - nothing to do, background tasks are in scheduler service
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
app.include_router(routers.service_deployments.router, prefix="/api")
app.include_router(routers.agent_configs.router, prefix="/api")
