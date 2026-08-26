"""Persist paid-work command identity and the owner-facing refusal text.

Revision ID: a3d4e5f6a7b8
Revises: f7e8d9c0b1a2
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a3d4e5f6a7b8"
down_revision: str | None = "f7e8d9c0b1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("work_admission_audits", sa.Column("command_payload", sa.JSON(), nullable=True))
    op.add_column("work_admission_audits", sa.Column("message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("work_admission_audits", "message")
    op.drop_column("work_admission_audits", "command_payload")
