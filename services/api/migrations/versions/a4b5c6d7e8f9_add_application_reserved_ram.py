"""Add persisted RAM reservations to applications.

Revision ID: a4b5c6d7e8f9
Revises: e2f3a4b5c6d7
Create Date: 2026-07-27 08:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a4b5c6d7e8f9"
down_revision: str | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "applications",
        sa.Column("reserved_ram_mb", sa.Integer(), nullable=False, server_default="512"),
    )
    op.alter_column("applications", "reserved_ram_mb", server_default=None)


def downgrade() -> None:
    op.drop_column("applications", "reserved_ram_mb")
