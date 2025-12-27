"""RAG search tools for context retrieval.

Provides tools for agents to search project documents, specs, and history.
"""

from http import HTTPStatus
from typing import Annotated

import httpx
from langchain_core.tools import tool
import structlog

from .base import api_client

logger = structlog.get_logger()


@tool
async def search_project_context(
    query: Annotated[str, "Search query for project context"],
    project_id: Annotated[str, "Project ID to search within"],
    user_id: Annotated[int | None, "User ID for access control"] = None,
) -> str:
    """Search project documents, specs, and history for relevant context.

    Use this tool when:
    - Answering questions about existing projects
    - Looking for past decisions or requirements
    - Finding implementation details or architecture info
    - Checking project history and what was discussed before

    Returns formatted context with source citations.
    """
    try:
        payload = {
            "query": query,
            "project_id": project_id,
            "user_id": user_id,
            "scope": "public",
            "top_k": 5,
            "max_tokens": 2000,
            "min_similarity": 0.25,  # Lower threshold for text-embedding-3-small
        }

        result = await api_client.post("/api/rag/query", json=payload)

        if not result.get("results"):
            return "No relevant context found for this query."

        # Format results with sources
        formatted_parts = ["## Relevant Context\n"]

        for i, chunk in enumerate(result["results"], 1):
            score = chunk.get("score", 0)
            source = chunk.get("source_id", "unknown")
            title = chunk.get("title", source)
            text = chunk.get("chunk_text", "")

            formatted_parts.append(
                f"### [{i}] {title} (relevance: {score:.2f})\nSource: `{source}`\n\n{text}\n\n---\n"
            )

        total_tokens = result.get("total_tokens", 0)
        truncated = result.get("truncated", False)
        if truncated:
            formatted_parts.append(f"\n*Results truncated at {total_tokens} tokens*")

        return "\n".join(formatted_parts)

    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == HTTPStatus.SERVICE_UNAVAILABLE:
            logger.warning("rag_search_unavailable", project_id=project_id)
            return "RAG search is temporarily unavailable."
        elif status == HTTPStatus.BAD_REQUEST:
            logger.warning(
                "rag_search_bad_request",
                project_id=project_id,
                detail=exc.response.text,
            )
            return f"Invalid search request: {exc.response.text}"
        else:
            logger.error(
                "rag_search_error",
                project_id=project_id,
                status_code=status,
            )
            return f"Search failed with status {status}"

    except Exception as exc:
        logger.error(
            "rag_search_exception",
            error=str(exc),
            error_type=type(exc).__name__,
            project_id=project_id,
        )
        return "An error occurred while searching. Please try again."


@tool
async def search_user_context(
    query: Annotated[str, "Search query"],
    user_id: Annotated[int, "User ID to search within"],
) -> str:
    """Search all user's documents across all projects.

    Use this for cross-project queries when the user asks about
    their overall history or patterns across multiple projects.
    """
    try:
        payload = {
            "query": query,
            "user_id": user_id,
            "scope": "user",
            "top_k": 5,
            "max_tokens": 2000,
            "min_similarity": 0.7,
        }

        result = await api_client.post("/api/rag/query", json=payload)

        if not result.get("results"):
            return "No relevant context found across your projects."

        formatted_parts = ["## Context from your projects\n"]

        for i, chunk in enumerate(result["results"], 1):
            score = chunk.get("score", 0)
            source = chunk.get("source_id", "unknown")
            title = chunk.get("title", source)
            text = chunk.get("chunk_text", "")

            formatted_parts.append(
                f"### [{i}] {title} (relevance: {score:.2f})\nSource: `{source}`\n\n{text}\n\n---\n"
            )

        return "\n".join(formatted_parts)

    except Exception as exc:
        logger.error(
            "rag_user_search_exception",
            error=str(exc),
            user_id=user_id,
        )
        return "An error occurred while searching. Please try again."
