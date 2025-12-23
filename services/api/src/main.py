"""API Service - FastAPI with SQLAlchemy."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from .database import engine
from .routers import health, resources


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    # Startup
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
