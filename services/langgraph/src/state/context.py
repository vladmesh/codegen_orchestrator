"""State context management for LangGraph tools.

Provides global state injection for tools that need access to the current
execution context (user_id, project_id, etc.) without explicit passing.
"""

from typing import Any

# Global state storage
_current_state: dict[str, Any] = {}
_redis_client: Any = None


def set_tool_context(state: dict[str, Any], redis_client: Any = None) -> None:
    """Set the current state context for tools.

    Called by ProductOwner node before executing tools.

    Args:
        state: Current execution state (user_id, project_id, etc.)
        redis_client: Optional Redis client instance
    """
    global _current_state, _redis_client
    _current_state = state
    _redis_client = redis_client


def get_current_state() -> dict[str, Any]:
    """Get current execution state for tools that need context.

    Returns:
        Dict containing current state (user_id, project_id, thread_id, etc.)
    """
    return _current_state


def get_redis_client() -> Any:
    """Get injected Redis client (if available).

    Returns:
        Redis client instance or None
    """
    return _redis_client
