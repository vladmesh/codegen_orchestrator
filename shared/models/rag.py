"""RAG index models."""

from datetime import datetime
from enum import Enum

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RAGScope(str, Enum):
    """RAG access scopes."""

    PUBLIC = "public"
    USER = "user"
    PROJECT = "project"


class RAGDocument(Base):
    """Indexed document metadata and content."""

    __tablename__ = "rag_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("projects.id"), nullable=True, index=True
    )
    scope: Mapped[str] = mapped_column(String(20), default=RAGScope.USER.value, index=True)

    source_type: Mapped[str] = mapped_column(String(50))
    source_id: Mapped[str] = mapped_column(String(255))
    source_uri: Mapped[str | None] = mapped_column(String(1024))
    source_hash: Mapped[str | None] = mapped_column(String(128))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    language: Mapped[str | None] = mapped_column(String(16))

    title: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    tsv: Mapped[str | None] = mapped_column(TSVECTOR)


class RAGChunk(Base):
    """Chunked content for retrieval."""

    __tablename__ = "rag_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(Integer, ForeignKey("rag_documents.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("projects.id"), nullable=True, index=True
    )
    scope: Mapped[str] = mapped_column(String(20), default=RAGScope.USER.value, index=True)

    chunk_index: Mapped[int] = mapped_column(Integer)
    chunk_text: Mapped[str] = mapped_column(Text)
    chunk_hash: Mapped[str | None] = mapped_column(String(128))
    token_count: Mapped[int | None] = mapped_column(Integer)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(512))
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    tsv: Mapped[str | None] = mapped_column(TSVECTOR)


class RAGConversationSummary(Base):
    """Summarized conversation context."""

    __tablename__ = "rag_conversation_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("projects.id"), nullable=True, index=True
    )
    thread_id: Mapped[str | None] = mapped_column(String(255), index=True)

    summary_text: Mapped[str] = mapped_column(Text)
    message_ids: Mapped[list[str] | None] = mapped_column(JSON)


class RAGMessage(Base):
    """Raw chat messages captured for summarization."""

    __tablename__ = "rag_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("projects.id"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    message_text: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(50), default="telegram")
    summarized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
