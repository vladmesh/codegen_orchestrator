"""RAG ingestion and message logging routes."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import hmac
import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog
import tiktoken

from shared.models import Project, RAGChunk, RAGDocument, RAGMessage, RAGScope, User

from ..database import get_async_session
from ..schemas.rag import (
    RAGDocsIngest,
    RAGDocsIngestResult,
    RAGDocumentPayload,
    RAGMessageCreate,
    RAGMessageRead,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/rag", tags=["rag"])

CHUNK_TOKEN_TARGET = 512
CHUNK_OVERLAP_TOKENS = 50
ENCODING_NAME = "cl100k_base"
MAX_SIGNATURE_SKEW_SECONDS = 5 * 60


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


@router.post("/ingest", response_model=RAGDocsIngestResult)
async def ingest_documents(
    request: Request,
    db: AsyncSession = Depends(get_async_session),
) -> RAGDocsIngestResult:
    """Ingest project docs from service-template webhook."""
    body = await request.body()
    _verify_ingest_signature(request, body)

    try:
        payload_dict = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON payload: {exc}",
        ) from exc

    try:
        payload = RAGDocsIngest.model_validate(payload_dict)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid payload schema: {exc}",
        ) from exc

    await _validate_payload_targets(db, payload)

    encoding = tiktoken.get_encoding(ENCODING_NAME)
    docs_indexed = 0
    docs_skipped = 0

    for doc in payload.documents:
        try:
            indexed = await _upsert_document(db, payload, doc, encoding)
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


def _verify_ingest_signature(request: Request, body: bytes) -> None:
    timestamp_header = request.headers.get("X-RAG-Timestamp")
    signature_header = request.headers.get("X-RAG-Signature")

    if not timestamp_header or not signature_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing RAG signature headers",
        )

    try:
        timestamp = int(timestamp_header)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-RAG-Timestamp",
        ) from exc

    now = int(time.time())
    if abs(now - timestamp) > MAX_SIGNATURE_SKEW_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="RAG signature timestamp out of range",
        )

    secret = os.getenv("RAG_INGEST_SECRET")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="RAG_INGEST_SECRET is not set",
        )

    if not signature_header.startswith("sha256="):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-RAG-Signature format",
        )

    expected = _build_signature(secret, timestamp, body)
    provided = signature_header.removeprefix("sha256=")
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid RAG signature",
        )


def _build_signature(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


async def _upsert_document(
    db: AsyncSession,
    payload: RAGDocsIngest,
    doc: RAGDocumentPayload,
    encoding: tiktoken.Encoding,
) -> bool:
    scope = _parse_scope(doc.scope)
    user_id, project_id = _resolve_scope_ids(payload, scope)

    existing = await db.execute(
        select(RAGDocument).where(
            RAGDocument.user_id == user_id,
            RAGDocument.project_id == project_id,
            RAGDocument.scope == scope.value,
            RAGDocument.source_type == doc.source_type,
            RAGDocument.source_id == doc.source_id,
        )
    )
    document = existing.scalar_one_or_none()

    incoming_hash = doc.content_hash or _hash_text(doc.content)
    if document:
        same_hash = document.source_hash == incoming_hash
        _apply_document_fields(document, doc, scope, user_id, project_id, incoming_hash)
        if same_hash:
            return False
    else:
        document = RAGDocument(
            user_id=user_id,
            project_id=project_id,
            scope=scope.value,
            source_type=doc.source_type,
            source_id=doc.source_id,
            source_uri=doc.source_uri,
            source_hash=incoming_hash,
            source_updated_at=doc.updated_at,
            language=doc.language,
            title=doc.title or doc.path or doc.source_id,
            body=doc.content,
            tsv=func.to_tsvector("simple", doc.content),
        )
        db.add(document)
        await db.flush()

    await db.execute(delete(RAGChunk).where(RAGChunk.document_id == document.id))

    chunk_texts = _chunk_document(doc.content, encoding)
    chunks = []
    for idx, chunk_text in enumerate(chunk_texts):
        chunks.append(
            RAGChunk(
                document_id=document.id,
                user_id=user_id,
                project_id=project_id,
                scope=scope.value,
                chunk_index=idx,
                chunk_text=chunk_text,
                chunk_hash=_hash_text(chunk_text),
                token_count=len(encoding.encode(chunk_text)),
                tsv=func.to_tsvector("simple", chunk_text),
            )
        )

    if chunks:
        db.add_all(chunks)
    return True


def _apply_document_fields(
    document: RAGDocument,
    doc: RAGDocumentPayload,
    scope: RAGScope,
    user_id: int | None,
    project_id: str | None,
    content_hash: str,
) -> None:
    document.user_id = user_id
    document.project_id = project_id
    document.scope = scope.value
    document.source_type = doc.source_type
    document.source_id = doc.source_id
    document.source_uri = doc.source_uri
    document.source_hash = content_hash
    document.source_updated_at = doc.updated_at
    document.language = doc.language
    document.title = doc.title or doc.path or doc.source_id
    document.body = doc.content
    document.tsv = func.to_tsvector("simple", doc.content)


def _parse_scope(scope: str) -> RAGScope:
    try:
        return RAGScope(scope)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid scope: {scope}",
        ) from exc


def _resolve_scope_ids(payload: RAGDocsIngest, scope: RAGScope) -> tuple[int | None, str | None]:
    if scope == RAGScope.PUBLIC:
        return None, None

    if payload.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_id is required for non-public scope",
        )

    if scope == RAGScope.PROJECT and payload.project_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project_id is required for project scope",
        )

    return payload.user_id, payload.project_id


async def _validate_payload_targets(db: AsyncSession, payload: RAGDocsIngest) -> None:
    scopes = {_parse_scope(doc.scope) for doc in payload.documents}
    needs_user = any(scope != RAGScope.PUBLIC for scope in scopes)
    needs_project = any(scope == RAGScope.PROJECT for scope in scopes)

    if needs_user:
        if payload.user_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="user_id is required for non-public scope",
            )
        if not await db.get(User, payload.user_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"user_id {payload.user_id} not found",
            )

    if needs_project:
        if payload.project_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="project_id is required for project scope",
            )
        if not await db.get(Project, payload.project_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"project_id {payload.project_id} not found",
            )


def _hash_text(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _chunk_document(text: str, encoding: tiktoken.Encoding) -> list[str]:
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    if not paragraphs:
        return []

    base_chunks: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0

    for paragraph in paragraphs:
        para_tokens = len(encoding.encode(paragraph))
        if para_tokens > CHUNK_TOKEN_TARGET:
            if current_parts:
                base_chunks.append("\n\n".join(current_parts))
                current_parts = []
                current_tokens = 0
            for slice_text in _split_long_paragraph(paragraph, encoding):
                base_chunks.append(slice_text)
            continue

        if current_tokens + para_tokens <= CHUNK_TOKEN_TARGET:
            current_parts.append(paragraph)
            current_tokens += para_tokens
        else:
            base_chunks.append("\n\n".join(current_parts))
            current_parts = [paragraph]
            current_tokens = para_tokens

    if current_parts:
        base_chunks.append("\n\n".join(current_parts))

    if CHUNK_OVERLAP_TOKENS <= 0:
        return base_chunks

    overlapped: list[str] = []
    prev_tokens: list[int] | None = None
    for base_chunk in base_chunks:
        chunk_text = base_chunk
        if prev_tokens:
            overlap_text = encoding.decode(prev_tokens[-CHUNK_OVERLAP_TOKENS:])
            chunk_text = f"{overlap_text}\n\n{base_chunk}"
        overlapped.append(chunk_text)
        prev_tokens = encoding.encode(base_chunk)

    return overlapped


def _split_long_paragraph(text: str, encoding: tiktoken.Encoding) -> Iterable[str]:
    tokens = encoding.encode(text)
    for start in range(0, len(tokens), CHUNK_TOKEN_TARGET):
        chunk_tokens = tokens[start : start + CHUNK_TOKEN_TARGET]
        yield encoding.decode(chunk_tokens)
