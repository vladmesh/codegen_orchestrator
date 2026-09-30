"""add story planning outcome

Revision ID: b7e1c3d5f9a2
Revises: a4d6f8b0c2e1
Create Date: 2026-09-26 20:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b7e1c3d5f9a2"
down_revision: str | None = "a4d6f8b0c2e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL: no planning outcome recorded yet, which is every existing story.
    op.add_column("stories", sa.Column("planning", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "planning")
