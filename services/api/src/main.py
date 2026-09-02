"""API Service - FastAPI with SQLAlchemy."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from http import HTTPStatus
import time
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import structlog

from shared.log_config import setup_logging
from shared.provisioning_policy import validate_provider_policies

from . import routers
from .database import engine
from .dependencies import close_redis, init_redis, require_authenticated_caller


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    setup_logging(service_name="api")
    validate_provider_policies()
    await init_redis()
    yield
    await close_redis()
    await engine.dispose()


# Authorization is one dependency on the application, not a decoration each
# router remembers to carry: an included router is closed whether or not whoever
# added it thought about it. The exceptions are listed in `ANONYMOUS_ROUTES`.
app = FastAPI(
    title="Codegen Orchestrator API",
    description="Internal API for database access",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(require_authenticated_caller)],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Log validation errors without copying request secrets into the log stream.

    `exc.errors()` already names the offending field and location, which is what
    debugging needs. The raw body and headers carry X-Internal-Key, LK bearer
    tokens and project secrets, and these logs are shipped to Loki.
    """
    logger = structlog.get_logger()

    try:
        body_size = len(await request.body())
    except Exception:
        body_size = -1

    logger.error(
        "validation_error",
        path=request.url.path,
        method=request.method,
        errors=exc.errors(),
        request_body_bytes=body_size,
    )

    # A model_validator raising ValueError puts the exception object itself into
    # the error ctx, which json.dumps cannot take. Encode before responding.
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({"detail": exc.errors(), "body": exc.body}),
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
        if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
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
# Queue introspection reads message bodies off the streams, so it belongs behind
# the same gate as the rest of the API rather than beside /health.
app.include_router(routers.debug.router, prefix="/api")
app.include_router(routers.admin_overview.router, prefix="/api")
app.include_router(routers.users.router, prefix="/api")
app.include_router(routers.projects.router, prefix="/api")
app.include_router(routers.promo_codes.router, prefix="/api")
app.include_router(routers.servers.router, prefix="/api")
app.include_router(routers.allocations.router, prefix="/api")
app.include_router(routers.api_keys.router, prefix="/api")
app.include_router(routers.analytics.router, prefix="/api")
app.include_router(routers.lk_auth.router, prefix="/api")
app.include_router(routers.lk.router, prefix="/api")
app.include_router(routers.incidents.router, prefix="/api")
app.include_router(routers.service_deployments.router, prefix="/api")
app.include_router(routers.applications.router, prefix="/api")
app.include_router(routers.agent_configs.router, prefix="/api")
app.include_router(routers.system_configs.router, prefix="/api")
app.include_router(routers.rag.router, prefix="/api")
app.include_router(routers.runs.router, prefix="/api")
app.include_router(routers.engineering_budget_policies.router, prefix="/api")
app.include_router(routers.engineering_budget_policies.self_router, prefix="/api")
app.include_router(routers.work_admission.router, prefix="/api")
app.include_router(routers.engineering_consumer.router, prefix="/api")
app.include_router(routers.tasks.router, prefix="/api")
app.include_router(routers.brainstorms.router, prefix="/api")
app.include_router(routers.repositories.router, prefix="/api")
app.include_router(routers.stories.router, prefix="/api")
app.include_router(routers.product_briefs.router, prefix="/api")
app.include_router(routers.temporary_access.router, prefix="/api")
