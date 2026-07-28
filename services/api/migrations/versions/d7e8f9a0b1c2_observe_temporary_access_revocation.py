"""Close a temporary access grant on a reading of the running service

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-07-28 22:30:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d7e8f9a0b1c2"
down_revision: str | None = "c6d7e8f9a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "temporary_access_grants"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("observation_id", sa.String(length=255), nullable=True))
    op.add_column(TABLE, sa.Column("slot_clear_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        TABLE,
        sa.Column("slot_clear_readings", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column(TABLE, "slot_clear_readings")
    op.drop_column(TABLE, "slot_clear_since")
    op.drop_column(TABLE, "observation_id")
    op.drop_column(TABLE, "observed_at")
