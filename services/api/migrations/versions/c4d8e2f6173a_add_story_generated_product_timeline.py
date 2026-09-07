"""add stories.generated_product_timeline

Revision ID: c4d8e2f6173a
Revises: b7c1e4a90d23
Create Date: 2026-09-07 14:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c4d8e2f6173a"
down_revision: str | None = "b7c1e4a90d23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("generated_product_timeline", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "generated_product_timeline")
