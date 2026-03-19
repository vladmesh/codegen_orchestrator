"""RAG vector search and token-budget helpers."""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.clients.embedding import generate_embeddings
from shared.models import RAGChunk, RAGDocument

from ..schemas.rag import RAGChunkResult

logger = structlog.get_logger()

EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_DIMENSIONS = 512


async def get_query_embedding(query: str) -> list[float]:
    """Generate a single embedding vector for a query string.

    Raises HTTPException(503) on failure.
    """
    from fastapi import HTTPException, status

    try:
        query_result = await generate_embeddings(
            [query],
            model=EMBEDDING_MODEL,
            dimensions=EMBEDDING_DIMENSIONS,
        )
        if not query_result.embeddings:
            raise ValueError("No embedding returned")
        return query_result.embeddings[0]
    except Exception as exc:
        logger.warning(
            "query_embedding_failed",
            error=str(exc),
            query_preview=query[:100],
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding service unavailable",
        ) from exc


async def search_chunks(
    db: AsyncSession,
    query_embedding: list[float],
    *,
    scope: str,
    user_id: int | None,
    project_id: uuid.UUID | None,
    top_k: int,
    min_similarity: float,
) -> list[tuple[RAGChunk, float, RAGDocument]]:
    """Search chunks by vector cosine similarity with scope filtering.

    Returns list of (chunk, similarity_score, document) tuples.
    """
    # Build scope filter conditions
    conditions = [RAGChunk.embedding.isnot(None)]

    if scope == "public":
        conditions.append(RAGChunk.scope == "public")
    elif scope == "user":
        conditions.append(RAGChunk.user_id == user_id)
        conditions.append(RAGChunk.scope.in_(["user", "public"]))
    elif scope == "project":
        conditions.append(RAGChunk.project_id == project_id)
        conditions.append(RAGChunk.scope.in_(["project", "user", "public"]))

    # Vector cosine similarity: 1 - cosine_distance
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
    similarity_expr = 1 - RAGChunk.embedding.cosine_distance(text(f"'{embedding_str}'::vector"))

    # Diagnostic query for candidates
    candidates_stmt = (
        select(similarity_expr.label("similarity"))
        .select_from(RAGChunk)
        .where(*conditions)
        .order_by(similarity_expr.desc())
        .limit(20)
    )
    candidates_result = await db.execute(candidates_stmt)
    candidate_scores = [row.similarity for row in candidates_result]

    if candidate_scores:
        logger.debug(
            "rag_search_candidates",
            scope=scope,
            project_id=project_id,
            min_similarity_threshold=min_similarity,
            candidates_count=len(candidate_scores),
            max_score=round(max(candidate_scores), 4),
            min_score=round(min(candidate_scores), 4),
            passing_count=sum(1 for s in candidate_scores if s >= min_similarity),
        )
    else:
        logger.debug(
            "rag_search_no_candidates",
            scope=scope,
            project_id=project_id,
        )

    # Filtered query
    stmt = (
        select(RAGChunk, similarity_expr.label("similarity"), RAGDocument)
        .join(RAGDocument, RAGChunk.document_id == RAGDocument.id)
        .where(*conditions)
        .where(similarity_expr >= min_similarity)
        .order_by(similarity_expr.desc())
        .limit(top_k)
    )

    result = await db.execute(stmt)
    return [(row.RAGChunk, row.similarity, row.RAGDocument) for row in result]


def apply_token_budget(
    results: list[tuple[RAGChunk, float, RAGDocument]],
    max_tokens: int,
) -> tuple[list[RAGChunkResult], int, bool]:
    """Apply token budget limit to search results.

    Returns (chunk_results, total_tokens, was_truncated).
    """
    chunk_results: list[RAGChunkResult] = []
    total_tokens = 0
    truncated = False

    for chunk, score, doc in results:
        chunk_tokens = chunk.token_count or 0
        if total_tokens + chunk_tokens > max_tokens and chunk_results:
            truncated = True
            break

        chunk_results.append(
            RAGChunkResult(
                chunk_text=chunk.chunk_text,
                score=score,
                source_type=doc.source_type,
                source_id=doc.source_id,
                source_uri=doc.source_uri,
                title=doc.title,
                chunk_index=chunk.chunk_index,
                token_count=chunk_tokens,
            )
        )
        total_tokens += chunk_tokens

    return chunk_results, total_tokens, truncated
