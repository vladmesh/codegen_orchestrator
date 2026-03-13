"""Langfuse tracing integration for LangChain/LangGraph.

Provides a callback handler factory that reads LANGFUSE_* env vars.
Returns an empty list when tracing is not configured (graceful no-op).
"""

import os

import structlog

logger = structlog.get_logger(__name__)


def get_langfuse_callbacks() -> list:
    """Return Langfuse callback handlers if env vars are configured.

    Required env vars (read by langfuse SDK automatically):
        LANGFUSE_PUBLIC_KEY: Langfuse project public key
        LANGFUSE_SECRET_KEY: Langfuse project secret key

    Optional env vars:
        LANGFUSE_HOST: Langfuse server URL (default: https://cloud.langfuse.com)

    Returns:
        List with one CallbackHandler if configured, empty list otherwise.
    """
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")

    if not public_key or not secret_key:
        logger.debug(
            "langfuse_tracing_disabled", reason="LANGFUSE_PUBLIC_KEY or SECRET_KEY not set"
        )
        return []

    from langfuse.langchain import CallbackHandler

    handler = CallbackHandler()
    host = os.environ.get("LANGFUSE_HOST", "")
    logger.info("langfuse_tracing_enabled", host=host or "(default cloud)")
    return [handler]
