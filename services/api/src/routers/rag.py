"""RAG ingestion and message logging routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.models import RAGConversationSummary, RAGMessage, User

from ..database import get_async_session
from ..schemas.rag import (
    RAGDocsIngestResult,
    RAGMessageCreate,
    RAGMessageRead,
    RAGQueryRequest,
    RAGQueryResult,
    RAGSummaryRead,
)
from .rag_ingest import (  # noqa: F401 – re-exported for backward compatibility
    CHUNK_OVERLAP_TOKENS,
    CHUNK_TOKEN_TARGET,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    ENCODING_NAME,
    MAX_SIGNATURE_SKEW_SECONDS,
    apply_document_fields,
    build_signature,
    chunk_document,
    generate_chunk_embeddings,
    get_encoding,
    hash_text,
    parse_scope,
    resolve_scope_ids,
    upsert_document,
    validate_payload_targets,
    verify_ingest_signature,
)
from .rag_search import (  # noqa: F401 – re-exported for backward compatibility
    apply_token_budget,
    get_query_embedding,
    search_chunks,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/rag", tags=["rag"])

# Backward-compatible aliases (old private names → new public names)
_verify_ingest_signature = verify_ingest_signature
_build_signature = build_signature
_get_encoding = get_encoding
_generate_chunk_embeddings = generate_chunk_embeddings
_search_chunks = search_chunks
_apply_token_budget = apply_token_budget
_upsert_document = upsert_document
_apply_document_fields = apply_document_fields
_parse_scope = parse_scope
_resolve_scope_ids = resolve_scope_ids
_validate_payload_targets = validate_payload_targets
_hash_text = hash_text
_chunk_document = chunk_document

__all__ = [
    # Router
    "router",
    # Constants
    "CHUNK_OVERLAP_TOKENS",
    "CHUNK_TOKEN_TARGET",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_MODEL",
    "ENCODING_NAME",
    "MAX_SIGNATURE_SKEW_SECONDS",
    # Ingest helpers
    "apply_document_fields",
    "build_signature",
    "chunk_document",
    "generate_chunk_embeddings",
    "get_encoding",
    "hash_text",
    "parse_scope",
    "resolve_scope_ids",
    "upsert_document",
    "validate_payload_targets",
    "verify_ingest_signature",
    # Search helpers
    "apply_token_budget",
    "get_query_embedding",
    "search_chunks",
]


@router.post("/messages", response_model=RAGMessageRead, status_code=status.HTTP_201_CREATED)
async def create_rag_message(
    message_in: RAGMessageCreate,
    db: AsyncSession = Depends(get_async_session),
) -> RAGMessage:
    """Store raw Telegram messages for summarization."""
    if message_in.user_id is None and message_in.telegram_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_id or telegram_id is required",
        )

    user = None
    if message_in.user_id is not None:
        user = await db.get(User, message_in.user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"user_id {message_in.user_id} not found",
            )
    else:
        query = select(User).where(User.telegram_id == message_in.telegram_id)
        result = await db.execute(query)
        user = result.scalar_one_or_none()
        if not user:
            user = User(telegram_id=message_in.telegram_id)
            db.add(user)
            await db.flush()

    rag_message = RAGMessage(
        user_id=user.id,
        project_id=message_in.project_id,
        role=message_in.role,
        message_text=message_in.message_text,
        message_id=message_in.message_id,
        source=message_in.source,
    )
    db.add(rag_message)
    await db.commit()
    await db.refresh(rag_message)
    return rag_message


@router.post("/query", response_model=RAGQueryResult)
async def query_rag(
    request: RAGQueryRequest,
    db: AsyncSession = Depends(get_async_session),
) -> RAGQueryResult:
    """Search RAG index for relevant context.

    Performs vector cosine similarity search with scope-based filtering.
    Returns chunks sorted by relevance, limited by token budget.
    """
    if request.scope == "project" and not request.project_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project_id is required for project scope",
        )
    if request.scope in ("project", "user") and not request.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_id is required for non-public scope",
        )

    query_embedding = await get_query_embedding(request.query)

    results = await search_chunks(
        db,
        query_embedding,
        scope=request.scope,
        user_id=request.user_id,
        project_id=request.project_id,
        top_k=request.top_k,
        min_similarity=request.min_similarity,
    )

    chunk_results, total_tokens, truncated = apply_token_budget(results, request.max_tokens)

    logger.info(
        "rag_query_completed",
        query_preview=request.query[:50],
        scope=request.scope,
        results_count=len(chunk_results),
        total_tokens=total_tokens,
        truncated=truncated,
    )

    return RAGQueryResult(
        query=request.query,
        results=chunk_results,
        total_tokens=total_tokens,
        truncated=truncated,
    )


@router.get("/summaries", response_model=list[RAGSummaryRead])
async def get_summaries(
    user_id: int,
    limit: int = 5,
    db: AsyncSession = Depends(get_async_session),
) -> list[RAGConversationSummary]:
    """Get recent conversation summaries for a user."""
    query = (
        select(RAGConversationSummary)
        .where(RAGConversationSummary.user_id == user_id)
        .order_by(RAGConversationSummary.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("/ingest", response_model=RAGDocsIngestResult)
async def ingest_documents(
    request: Request,
    db: AsyncSession = Depends(get_async_session),
) -> RAGDocsIngestResult:
    """Ingest project docs from service-template webhook."""
    body = await request.body()
    verify_ingest_signature(request, body)

    try:
        payload_dict = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON payload: {exc}",
        ) from exc

    from ..schemas.rag import RAGDocsIngest

    try:
        payload = RAGDocsIngest.model_validate(payload_dict)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid payload schema: {exc}",
        ) from exc

    await validate_payload_targets(db, payload)

    encoding = get_encoding()
    docs_indexed = 0
    docs_skipped = 0

    for doc in payload.documents:
        try:
            indexed = await upsert_document(db, payload, doc, encoding)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "rag_ingest_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                source_id=doc.source_id,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to ingest documents",
            ) from exc

        if indexed:
            docs_indexed += 1
        else:
            docs_skipped += 1

    await db.commit()

    return RAGDocsIngestResult(
        documents_received=len(payload.documents),
        documents_indexed=docs_indexed,
        documents_skipped=docs_skipped,
    )
