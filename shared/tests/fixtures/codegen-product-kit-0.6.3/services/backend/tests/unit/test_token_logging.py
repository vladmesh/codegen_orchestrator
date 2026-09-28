"""Backend startup also protects rendered foreign HTTP exception diagnostics."""

from __future__ import annotations

import io
import logging
import sys

import httpx
import pytest
import structlog

from services.backend.src.core.logging import configure_logging
from services.backend.src.core.settings import get_settings

TOKEN = "123456789:backend_canary_token_abcdefghijklmnopqrstuvwxyz"  # noqa: S105


@pytest.mark.asyncio
@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("console", [False, True])
async def test_backend_startup_redacts_http_failure_output(
    monkeypatch: pytest.MonkeyPatch, debug: bool, console: bool
) -> None:
    output = io.StringIO()
    monkeypatch.setattr(output, "isatty", lambda: console)
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(get_settings(), "debug", debug)
    root = logging.getLogger()
    handlers, root_level = root.handlers[:], root.level
    levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        configure_logging()
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                logging.getLogger("httpx").warning("telegram_request_failed", exc_info=True)
                structlog.get_logger().exception("service_request_failed")
        rendered = output.getvalue()
        assert TOKEN not in rendered
        assert "telegram_request_failed" in rendered
        assert "service_request_failed" in rendered
        assert "HTTPStatusError" in rendered
        assert "sendMessage" in rendered
        assert "503" in rendered
        assert get_settings().app_name in rendered
    finally:
        root.handlers[:] = handlers
        root.setLevel(root_level)
        for name, previous in levels.items():
            logging.getLogger(name).setLevel(previous)
        structlog.contextvars.clear_contextvars()
