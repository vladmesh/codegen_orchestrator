"""Base classes for infrastructure worker nodes.

Simplified versions of LangGraph node bases, without LLM dependencies.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
import time
from typing import Any, TypeVar

import structlog

logger = structlog.get_logger()

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """Retry policy for nodes."""

    max_attempts: int = 1
    backoff_seconds: float = 0.0


class BaseNode:
    """Base node class."""

    def __init__(self, node_id: str | None = None, timeout_seconds: int | None = None):
        self.node_id = node_id
        self.timeout_seconds = timeout_seconds


class FunctionalNode(BaseNode):
    """Deterministic node without LLM involvement."""

    def __init__(
        self,
        node_id: str,
        timeout_seconds: int | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        super().__init__(node_id=node_id, timeout_seconds=timeout_seconds)
        self.retry_policy = retry_policy or RetryPolicy()


def log_node_execution(node_name: str) -> Callable:
    """Decorator to log node start/end with structured logging.

    Args:
        node_name: Name of the node for logging context.

    Returns:
        Decorated async function with structured logging.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            state: dict | None = None
            if args:
                if isinstance(args[0], dict):
                    state = args[0]
                elif len(args) > 1 and isinstance(args[1], dict):
                    state = args[1]

            if state is None:
                state = kwargs.get("state") if isinstance(kwargs.get("state"), dict) else {}

            preexisting_context = structlog.contextvars.get_contextvars()
            bound_thread = False

            bind_kwargs: dict[str, Any] = {"node": node_name}
            thread_id = state.get("thread_id") if isinstance(state, dict) else None
            if thread_id and "thread_id" not in preexisting_context:
                bind_kwargs["thread_id"] = thread_id
                bound_thread = True

            structlog.contextvars.bind_contextvars(**bind_kwargs)

            logger.info("node_start")
            start = time.time()

            try:
                result = await func(*args, **kwargs)
                duration = (time.time() - start) * 1000

                state_updates = list(result.keys()) if isinstance(result, dict) else []
                logger.info(
                    "node_complete",
                    duration_ms=round(duration, 2),
                    state_updates=state_updates,
                )

                return result

            except Exception as e:
                duration = (time.time() - start) * 1000

                logger.error(
                    "node_failed",
                    duration_ms=round(duration, 2),
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True,
                )
                raise
            finally:
                structlog.contextvars.unbind_contextvars("node")
                if bound_thread:
                    structlog.contextvars.unbind_contextvars("thread_id")

        return wrapper

    return decorator
