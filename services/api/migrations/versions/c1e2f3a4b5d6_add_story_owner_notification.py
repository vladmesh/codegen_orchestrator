"""add story owner notification

Revision ID: c1e2f3a4b5d6
Revises: b7c8d9e0f1a2
Create Date: 2026-08-29 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c1e2f3a4b5d6"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("owner_notification", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "owner_notification")
