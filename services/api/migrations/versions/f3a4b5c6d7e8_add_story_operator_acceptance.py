"""add story operator acceptance and reopen marker

Revision ID: f3a4b5c6d7e8
Revises: c1e2f3a4b5d6
Create Date: 2026-08-29 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f3a4b5c6d7e8"
down_revision: str | None = "c1e2f3a4b5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("operator_acceptance", sa.JSON(), nullable=True))
    op.add_column("stories", sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "reopened_at")
    op.drop_column("stories", "operator_acceptance")
