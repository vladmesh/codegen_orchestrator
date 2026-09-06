"""Request logging middleware for FastAPI.

Logs every request/response as a structured JSON line with standard fields.
Unhandled exceptions are logged with traceback before returning 500.
"""

from __future__ import annotations

from collections.abc import Callable
import time

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp
import structlog

logger = structlog.stdlib.get_logger()

# ---------------------------------------------------------------------------
# Health-check paths excluded from request logging (too noisy)
# ---------------------------------------------------------------------------
SILENT_PATHS: set[str] = {"/health", "/healthz", "/readyz"}


# ---------------------------------------------------------------------------
# User-ID extractor — override in create_app() to add auth-based extraction
# ---------------------------------------------------------------------------
def default_user_id_extractor(request: Request) -> str | None:  # noqa: ARG001
    """Return ``None`` by default.  Replace with your auth logic."""
    return None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log method, path, status, duration and optional user_id for every request."""

    def __init__(
        self,
        app: ASGIApp,
        user_id_extractor: Callable[[Request], str | None] | None = None,
    ) -> None:
        super().__init__(app)
        self._extract_user_id = user_id_extractor or default_user_id_extractor

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in SILENT_PATHS:
            return await call_next(request)

        start = time.perf_counter()
        user_id = self._extract_user_id(request)

        structlog.contextvars.bind_contextvars(
            method=request.method,
            path=request.url.path,
            user_id=user_id,
        )

        try:
            response = await call_next(request)
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                "request",
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
            return response
        except Exception as exc:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.error(
                "unhandled_exception",
                duration_ms=duration_ms,
                exception_type=type(exc).__name__,
                exception_message=str(exc),
                exc_info=exc,
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error"},
            )
        finally:
            structlog.contextvars.unbind_contextvars("method", "path", "user_id")
