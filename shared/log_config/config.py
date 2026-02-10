import logging
import os
import sys
from typing import Literal

import structlog
from structlog.types import Processor


def setup_logging(
    service_name: str | None = None,
    log_format: Literal["json", "console"] | None = None,
    log_level: str | None = None,
) -> None:
    """Configure structured logging with structlog.

    Args:
        service_name: Name of the service (e.g., "api", "langgraph").
                     Falls back to SERVICE_NAME env var or "unknown".
        log_format: Output format - "json" for production, "console" for dev.
                   Falls back to LOG_FORMAT env var or "console".
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR).
                  Falls back to LOG_LEVEL env var or "INFO".
    """
    # Get configuration from args or environment
    service_name = service_name or os.getenv("SERVICE_NAME", "unknown")
    log_format = log_format or os.getenv("LOG_FORMAT", "console")
    log_level = log_level or os.getenv("LOG_LEVEL", "INFO")

    # Convert log level string to logging constant
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Configure standard library logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )

    # Common processors for all formats
    processors: list[Processor] = [
        # Add log level
        structlog.stdlib.add_log_level,
        # Add logger name
        structlog.stdlib.add_logger_name,
        # Add timestamp
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        # Merge contextvars (for correlation_id, thread_id, etc.)
        structlog.contextvars.merge_contextvars,
        # Add service name to all logs
        structlog.processors.CallsiteParameterAdder(
            {
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            }
        ),
        # Stack info for exceptions
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Choose renderer based on format
    if log_format == "json":
        # JSON output for production/Grafana
        processors.append(structlog.processors.JSONRenderer(sort_keys=False))
    else:
        # Console output for development
        processors.append(
            structlog.dev.ConsoleRenderer(
                colors=True,
                exception_formatter=structlog.dev.plain_traceback,
            )
        )

    # Configure structlog
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Bind service name to all logs
    structlog.contextvars.bind_contextvars(service=service_name)

    # Log initialization
    logger = structlog.get_logger()
    logger.info(
        "logging_initialized",
        service=service_name,
        log_format=log_format,
        log_level=log_level,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger instance.

    Args:
        name: Optional logger name. If not provided, uses the caller's module name.

    Returns:
        Configured structlog logger instance.
    """
    return structlog.get_logger(name)
