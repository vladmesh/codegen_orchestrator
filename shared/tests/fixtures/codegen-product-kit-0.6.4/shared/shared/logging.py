"""Structured logging configuration for all services.

Provides a single ``configure_logging`` entry-point that every service calls
at startup.  Output is JSON on stdout (for Docker / log collectors) when
running non-interactively, and coloured console output in a terminal.
"""

from __future__ import annotations

import logging
import re
import sys

import structlog

_TELEGRAM_TOKEN_PATH = re.compile(r"(/(?:file/)?bot)\d+(?::|%3[Aa])[A-Za-z0-9_-]+")


class _TelegramSafeFormatter(structlog.stdlib.ProcessorFormatter):
    """Remove Telegram URL credentials after rendering messages and tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return _TELEGRAM_TOKEN_PATH.sub(r"\1[REDACTED]", super().format(record))


def configure_logging(service_name: str, log_level: str = "INFO") -> None:
    """Set up *structlog* with JSON output and bind the service name globally."""

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", key="timestamp"),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer()
        if sys.stdout.isatty()
        else structlog.processors.JSONRenderer()
    )

    formatter = _TelegramSafeFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Request URLs contain Telegram credentials, including at application DEBUG.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Bind service name so every subsequent log line includes it
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service_name)
