"""add plan to work_items

Revision ID: 7a8b9c0d1e2f
Revises: 624b334cdb34
Create Date: 2026-03-07 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "7a8b9c0d1e2f"
down_revision: str | None = "624b334cdb34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("work_items", sa.Column("plan", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("work_items", "plan")
