import structlog


def set_correlation_id(correlation_id: str) -> None:
    """Set correlation ID for current context."""
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)


def get_correlation_id() -> str | None:
    """Get correlation ID from current context."""
    return structlog.contextvars.get_contextvars().get("correlation_id")


def clear_context() -> None:
    """Clear all context variables."""
    structlog.contextvars.clear_contextvars()
