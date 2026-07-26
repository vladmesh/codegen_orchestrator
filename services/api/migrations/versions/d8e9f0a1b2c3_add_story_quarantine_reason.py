"""add story quarantine reason

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-07-26 05:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d8e9f0a1b2c3"
down_revision: str | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("quarantine_reason", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "quarantine_reason")
