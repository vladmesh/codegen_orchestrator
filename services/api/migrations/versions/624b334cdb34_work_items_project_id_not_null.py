"""work_items project_id not null

Revision ID: 624b334cdb34
Revises: a0b1c2d3e4f5
Create Date: 2026-03-07 01:39:27.400047

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "624b334cdb34"
down_revision: str | None = "a0b1c2d3e4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "work_items", "project_id", existing_type=sa.VARCHAR(length=255), nullable=False
    )


def downgrade() -> None:
    op.alter_column("work_items", "project_id", existing_type=sa.VARCHAR(length=255), nullable=True)
