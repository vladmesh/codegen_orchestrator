from typing import Any

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


_MESSAGE_CONTEXT_KEYS = ("correlation_id", "task_id", "story_id", "project_id", "request_id")


def bind_message_context(data: dict[str, Any]) -> None:
    """Bind correlation context from a queue message to structlog contextvars.

    Extracts correlation_id and common identifiers (task_id, story_id, project_id)
    from the message data and binds them for all subsequent log lines.
    """
    bindings: dict[str, str] = {}
    for key in _MESSAGE_CONTEXT_KEYS:
        value = data.get(key)
        if value:
            bindings[key] = value
    if bindings:
        structlog.contextvars.bind_contextvars(**bindings)


def unbind_message_context() -> None:
    """Remove message-specific context keys without clearing service-level bindings."""
    structlog.contextvars.unbind_contextvars(*_MESSAGE_CONTEXT_KEYS)
