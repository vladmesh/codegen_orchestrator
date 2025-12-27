"""RAG schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class RAGMessageCreate(BaseModel):
    """Schema for creating a raw RAG message."""

    user_id: int | None = None
    telegram_id: int | None = None
    project_id: str | None = None
    role: Literal["user", "assistant"]
    message_text: str
    message_id: str | None = None
    source: str = "telegram"


class RAGMessageRead(BaseModel):
    """Schema for reading a raw RAG message."""

    id: int
    user_id: int
    project_id: str | None = None
    role: str
    message_text: str
    message_id: str | None = None
    source: str
    summarized_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class RAGRepoInfo(BaseModel):
    """Repository metadata for RAG ingestion."""

    full_name: str
    ref: str
    commit_sha: str


class RAGDocumentPayload(BaseModel):
    """Document payload for RAG ingestion."""

    source_type: str
    source_id: str
    source_uri: str | None = None
    scope: str
    path: str | None = None
    title: str | None = None
    content: str
    language: str | None = None
    updated_at: datetime | None = None
    content_hash: str | None = None


class RAGDocsIngest(BaseModel):
    """Webhook payload for RAG document ingestion."""

    event: str | None = None
    project_id: str | None = None
    user_id: int | None = None
    repo: RAGRepoInfo | None = None
    documents: list[RAGDocumentPayload]


class RAGDocsIngestResult(BaseModel):
    """Ingestion response summary."""

    documents_received: int
    documents_indexed: int
    documents_skipped: int


# --- Query schemas ---


class RAGQueryRequest(BaseModel):
    """Request for RAG search."""

    query: str
    user_id: int | None = None
    project_id: str | None = None
    scope: Literal["project", "user", "public"] = "project"
    top_k: int = 5
    max_tokens: int = 2000
    min_similarity: float = 0.7


class RAGChunkResult(BaseModel):
    """Single chunk result from RAG search."""

    chunk_text: str
    score: float
    source_type: str
    source_id: str
    source_uri: str | None = None
    title: str | None = None
    chunk_index: int
    token_count: int | None = None


class RAGQueryResult(BaseModel):
    """RAG search response."""

    query: str
    results: list[RAGChunkResult]
    total_tokens: int
    truncated: bool = False
