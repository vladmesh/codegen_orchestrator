"""add_rag_messages_table

Revision ID: c9f7e3d4c2ab
Revises: b4b9e18d6e32
Create Date: 2026-01-05 13:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c9f7e3d4c2ab"
down_revision: str | None = "b4b9e18d6e32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rag_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("summarized_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(op.f("ix_rag_messages_user_id"), "rag_messages", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_rag_messages_project_id"),
        "rag_messages",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_rag_messages_summarized_at"),
        "rag_messages",
        ["summarized_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("rag_messages")
