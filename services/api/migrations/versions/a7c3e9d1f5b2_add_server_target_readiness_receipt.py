"""Record a managed target's key fingerprint and QA target readiness receipt

Revision ID: a7c3e9d1f5b2
Revises: d9e4f2a1b6c3
Create Date: 2026-09-13 22:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a7c3e9d1f5b2"
down_revision: str | None = "d9e4f2a1b6c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "servers"


def upgrade() -> None:
    # Existing rows get no receipt: admission refuses them until the first
    # reconciliation proves the current QA target profile on each one.
    op.add_column(TABLE, sa.Column("ssh_key_fingerprint", sa.String(length=100), nullable=True))
    op.add_column(TABLE, sa.Column("qa_target_version", sa.String(length=64), nullable=True))
    op.add_column(TABLE, sa.Column("qa_target_proved_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column(TABLE, "qa_target_proved_at")
    op.drop_column(TABLE, "qa_target_version")
    op.drop_column(TABLE, "ssh_key_fingerprint")
