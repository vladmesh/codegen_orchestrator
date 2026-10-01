"""Add the per-application monitoring switch.

Every existing application keeps being monitored: the column is NOT NULL with a
server default of true, so the backfill is the default itself.

Revision ID: c3e5a7b9d1f2
Revises: b2d4f6a8c0e1
Create Date: 2026-10-01 18:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c3e5a7b9d1f2"
down_revision: str | None = "b2d4f6a8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "applications",
        sa.Column("monitoring_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "applications",
        sa.Column("monitoring_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "applications",
        sa.Column("monitoring_changed_by", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("applications", "monitoring_changed_by")
    op.drop_column("applications", "monitoring_changed_at")
    op.drop_column("applications", "monitoring_enabled")
