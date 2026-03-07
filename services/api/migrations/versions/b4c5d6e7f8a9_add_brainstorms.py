"""Add brainstorms table and source_brainstorm_id to work_items

Revision ID: b4c5d6e7f8a9
Revises: 7a8b9c0d1e2f
Create Date: 2026-03-07 03:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b4c5d6e7f8a9"
down_revision: str | None = "7a8b9c0d1e2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "brainstorms",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(255),
            sa.ForeignKey("projects.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="draft", index=True),
        sa.Column("created_by", sa.String(50), nullable=False, server_default="system"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.add_column(
        "work_items",
        sa.Column(
            "source_brainstorm_id",
            sa.String(255),
            sa.ForeignKey("brainstorms.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("work_items", "source_brainstorm_id")
    op.drop_table("brainstorms")
