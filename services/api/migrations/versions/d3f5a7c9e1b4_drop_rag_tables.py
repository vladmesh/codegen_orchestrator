"""drop rag tables

RAG is removed from the product: nothing reads or writes these tables any more.
Dropping a table drops its indexes and the foreign keys it owns; the only foreign
key between them (rag_chunks -> rag_documents) goes with rag_chunks. The pgvector
extension is left installed: the historical add_rag_tables revision still creates
it on a fresh database, and dropping it would need a privilege this role may lack.

Revision ID: d3f5a7c9e1b4
Revises: b7e1c3d5f9a2
Create Date: 2026-09-27 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d3f5a7c9e1b4"
down_revision: str | None = "b7e1c3d5f9a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Children before parents, so no drop waits on a foreign key still pointing at it.
RAG_TABLES = ("rag_chunks", "rag_documents", "rag_conversation_summaries", "rag_messages")


def upgrade() -> None:
    for table in RAG_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")


def downgrade() -> None:
    raise NotImplementedError(
        "d3f5a7c9e1b4 dropped the RAG tables and their rows; RAG no longer exists in the "
        "code, so there is nothing to recreate them for. Restore from the pre-migration "
        "pg_dump if the rows are needed."
    )
