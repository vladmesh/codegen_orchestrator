"""Retain closed durable grant-intent retry epochs.

Revision ID: b9c0d1e2f3a4
Revises: a8c1d2e3f4a5
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b9c0d1e2f3a4"
down_revision: str | None = "a8c1d2e3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users_grant_intents",
        sa.Column("retry_history", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users_grant_intents", "retry_history")
