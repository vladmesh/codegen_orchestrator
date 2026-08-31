"""Record bounded grant-operation recovery attempts.

Revision ID: e1f2a3b4c5d6
Revises: c0d1e2f3a4b5
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "c0d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "temporary_access_grants",
        sa.Column("grant_attempts", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("temporary_access_grants", "grant_attempts", server_default=None)


def downgrade() -> None:
    op.drop_column("temporary_access_grants", "grant_attempts")
