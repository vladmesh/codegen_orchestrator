"""Drop legacy Run token and cost compatibility columns.

Revision ID: d9e4f2a1b6c3
Revises: c4d8e2f6173a
Create Date: 2026-09-09 10:35:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d9e4f2a1b6c3"
down_revision: str | None = "c4d8e2f6173a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LEGACY_RUN_ACCOUNTING_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
)


def upgrade() -> None:
    for column in _LEGACY_RUN_ACCOUNTING_COLUMNS:
        op.drop_column("runs", column)


def downgrade() -> None:
    op.add_column("runs", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("total_tokens", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("cost_usd", sa.Float(), nullable=True))
