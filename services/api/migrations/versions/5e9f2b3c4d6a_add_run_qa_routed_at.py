"""Record on a QA run when its story consumed the verdict

Revision ID: 5e9f2b3c4d6a
Revises: 4d8e1f2a3b5c
Create Date: 2026-09-22 20:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "5e9f2b3c4d6a"
down_revision: str | None = "4d8e1f2a3b5c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "runs"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("qa_routed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column(TABLE, "qa_routed_at")
