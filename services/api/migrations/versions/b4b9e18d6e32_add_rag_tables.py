"""add_rag_tables

Revision ID: b4b9e18d6e32
Revises: 9a92199fda5f
Create Date: 2026-01-05 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b4b9e18d6e32"
down_revision: str | None = "9a92199fda5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    op.create_table(
        "rag_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.String(length=255), nullable=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("source_uri", sa.String(length=1024), nullable=True),
        sa.Column("source_hash", sa.String(length=128), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("tsv", postgresql.TSVECTOR(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "rag_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.String(length=255), nullable=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("chunk_hash", sa.String(length=128), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("embedding", Vector(512), nullable=True),
        sa.Column("embedding_model", sa.String(length=128), nullable=True),
        sa.Column("tsv", postgresql.TSVECTOR(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["rag_documents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "rag_conversation_summaries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=True),
        sa.Column("thread_id", sa.String(length=255), nullable=True),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("message_ids", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(op.f("ix_rag_documents_user_id"), "rag_documents", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_rag_documents_project_id"),
        "rag_documents",
        ["project_id"],
        unique=False,
    )
    op.create_index(op.f("ix_rag_documents_scope"), "rag_documents", ["scope"], unique=False)

    op.create_index(op.f("ix_rag_chunks_document_id"), "rag_chunks", ["document_id"], unique=False)
    op.create_index(op.f("ix_rag_chunks_user_id"), "rag_chunks", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_rag_chunks_project_id"),
        "rag_chunks",
        ["project_id"],
        unique=False,
    )
    op.create_index(op.f("ix_rag_chunks_scope"), "rag_chunks", ["scope"], unique=False)

    op.create_index(
        op.f("ix_rag_conversation_summaries_user_id"),
        "rag_conversation_summaries",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_rag_conversation_summaries_project_id"),
        "rag_conversation_summaries",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_rag_conversation_summaries_thread_id"),
        "rag_conversation_summaries",
        ["thread_id"],
        unique=False,
    )

    op.execute("CREATE INDEX rag_chunks_tsv_idx ON rag_chunks USING GIN (tsv);")
    op.execute(
        "CREATE INDEX rag_chunks_embedding_idx "
        "ON rag_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);"
    )


def downgrade() -> None:
    op.drop_table("rag_conversation_summaries")
    op.drop_table("rag_chunks")
    op.drop_table("rag_documents")
