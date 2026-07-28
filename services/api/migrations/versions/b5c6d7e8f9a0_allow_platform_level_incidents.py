"""Allow platform-level incidents without a server

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-07-28 09:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b5c6d7e8f9a0"
down_revision: str | None = "a4b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "incidents",
        "server_handle",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade() -> None:
    op.execute("DELETE FROM incidents WHERE server_handle IS NULL")
    op.alter_column(
        "incidents",
        "server_handle",
        existing_type=sa.String(length=255),
        nullable=False,
    )
