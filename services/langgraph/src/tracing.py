"""Langfuse tracing integration for LangChain/LangGraph.

Provides a callback handler factory that reads LANGFUSE_* env vars.
Returns an empty list when tracing is not configured (graceful no-op).

Metadata helper builds LangChain-compatible metadata dict with Langfuse
special keys (langfuse_user_id, langfuse_session_id, langfuse_tags).
"""

import os
from typing import Any

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


def build_langfuse_metadata(
    *,
    agent_type: str,
    user_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    story_id: str | None = None,
) -> dict[str, Any]:
    """Build LangChain metadata dict with Langfuse-recognized keys.

    Langfuse CallbackHandler reads these special keys from LangChain metadata:
        - langfuse_user_id: maps to Langfuse trace user_id
        - langfuse_session_id: maps to Langfuse trace session_id (grouped by project)
        - langfuse_tags: list of string tags for filtering

    All other keys are stored as custom metadata on the trace.
    Pass the returned dict as config["metadata"] when invoking a LangGraph graph.
    """
    metadata: dict[str, Any] = {}
    tags = [f"agent:{agent_type}"]

    if user_id:
        metadata["langfuse_user_id"] = str(user_id)

    if project_id:
        metadata["langfuse_session_id"] = project_id
        tags.append(f"project:{project_id}")

    if task_id:
        metadata["task_id"] = task_id

    if story_id:
        metadata["story_id"] = story_id

    metadata["langfuse_tags"] = tags
    metadata["agent_type"] = agent_type

    return metadata
