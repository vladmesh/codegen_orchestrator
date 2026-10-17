"""Tests for request logging middleware and exception handler."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
import io
import json
import logging

from fastapi import FastAPI, Request, status
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
import structlog

from shared.logging import configure_logging

configure_logging(service_name="backend_test")


@pytest.fixture()
def log_capture() -> Generator[io.StringIO, None, None]:
    """Capture structlog JSON output via a dedicated handler."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=[
                structlog.contextvars.merge_contextvars,
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", key="timestamp"),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
            ],
        )
    )
    root = logging.getLogger()
    root.addHandler(handler)
    yield buf
    root.removeHandler(handler)


def _parse_log_lines(output: str, event_name: str) -> list[dict]:
    """Extract JSON log lines matching the given event name."""
    results = []
    for line in output.strip().splitlines():
        try:
            parsed = json.loads(line)
            if parsed.get("event") == event_name:
                results.append(parsed)
        except json.JSONDecodeError:
            continue
    return results


@pytest.fixture()
def log_app() -> FastAPI:
    """Minimal FastAPI app with logging middleware wired in."""

    from services.backend.src.app.middleware import RequestLoggingMiddleware

    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/hello")
    async def _hello() -> dict[str, str]:
        return {"msg": "hi"}

    @app.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("test explosion")

    return app


@pytest_asyncio.fixture()
async def log_client(log_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=log_app),
        base_url="http://test",
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_request_logged_with_standard_fields(
    log_client: AsyncClient, log_capture: io.StringIO
) -> None:
    response = await log_client.get("/hello")

    assert response.status_code == status.HTTP_200_OK

    output = log_capture.getvalue()
    request_logs = _parse_log_lines(output, "request")

    assert len(request_logs) >= 1, f"Expected request log, got: {output}"
    log = request_logs[-1]
    assert log["method"] == "GET"
    assert log["path"] == "/hello"
    assert log["status_code"] == status.HTTP_200_OK
    assert "duration_ms" in log
    assert "timestamp" in log


@pytest.mark.asyncio
async def test_health_endpoint_not_logged(
    log_client: AsyncClient, log_capture: io.StringIO
) -> None:
    response = await log_client.get("/health")

    assert response.status_code == status.HTTP_200_OK

    output = log_capture.getvalue()
    health_logs = _parse_log_lines(output, "request")
    for log in health_logs:
        assert log.get("path") != "/health", "Health endpoint should not be logged"


@pytest.mark.asyncio
async def test_exception_returns_500_and_logs_error(
    log_client: AsyncClient, log_capture: io.StringIO
) -> None:
    response = await log_client.get("/boom")

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json() == {"detail": "Internal server error"}

    output = log_capture.getvalue()
    error_logs = _parse_log_lines(output, "unhandled_exception")

    assert len(error_logs) >= 1, f"Expected error log, got: {output}"
    log = error_logs[-1]
    assert log["exception_type"] == "RuntimeError"
    assert "test explosion" in log["exception_message"]


@pytest.mark.asyncio
async def test_user_id_extractor_is_called(log_capture: io.StringIO) -> None:
    from services.backend.src.app.middleware import (
        RequestLoggingMiddleware,
    )

    app = FastAPI()

    def extract_user(request: Request) -> str | None:
        return "user:42"

    app.add_middleware(RequestLoggingMiddleware, user_id_extractor=extract_user)

    @app.get("/me")
    async def _me() -> dict[str, str]:
        return {"user": "me"}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        await c.get("/me")

    output = log_capture.getvalue()
    request_logs = _parse_log_lines(output, "request")

    assert len(request_logs) >= 1, f"No request log found: {output}"
    assert request_logs[-1]["user_id"] == "user:42"
