"""API Service - FastAPI with SQLAlchemy."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import time
import uuid

from fastapi import FastAPI, Request
import structlog

from shared.logging_config import setup_logging

from . import routers
from .database import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    setup_logging(service_name="api")
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


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", f"req_{uuid.uuid4().hex[:8]}")
    structlog.contextvars.bind_contextvars(
        correlation_id=correlation_id, method=request.method, path=request.url.path
    )

    start = time.time()
    logger = structlog.get_logger()

    try:
        response = await call_next(request)
        duration_ms = (time.time() - start) * 1000

        # Log 4xx and 5xx as errors/warnings
        if response.status_code >= 500:  # noqa: PLR2004
            logger.error(
                "http_request_failed",
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )
        else:
            logger.info(
                "http_request", status_code=response.status_code, duration_ms=round(duration_ms, 2)
            )

        return response
    except Exception as e:
        duration_ms = (time.time() - start) * 1000
        logger.error(
            "http_request_exception",
            error=str(e),
            error_type=type(e).__name__,
            duration_ms=round(duration_ms, 2),
            exc_info=True,
        )
        raise
    finally:
        structlog.contextvars.clear_contextvars()


@app.get("/")
async def root():
    """Root endpoint - API information."""
    return {
        "name": "Codegen Orchestrator API",
        "version": "0.1.0",
        "description": "Internal API for database access",
    }


app.include_router(routers.health.router)
app.include_router(routers.resources.router, prefix="/api")
app.include_router(routers.users.router, prefix="/api")
app.include_router(routers.projects.router, prefix="/api")
app.include_router(routers.servers.router, prefix="/api")
app.include_router(routers.api_keys.router, prefix="/api")
app.include_router(routers.incidents.router, prefix="/api")
app.include_router(routers.service_deployments.router, prefix="/api")
app.include_router(routers.agent_configs.router, prefix="/api")
app.include_router(routers.cli_agent_configs.router, prefix="/api")
app.include_router(routers.available_models.router, prefix="/api")
