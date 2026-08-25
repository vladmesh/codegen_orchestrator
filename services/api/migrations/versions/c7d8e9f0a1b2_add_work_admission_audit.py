"""Add count-based work admission audit records.

Revision ID: c7d8e9f0a1b2
Revises: f6e7d8c9b0a1
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "f6e7d8c9b0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "work_admission_audits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("reference_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_work_admission_audits_subject", "work_admission_audits", ["subject"])
    op.create_index("ix_work_admission_audits_outcome", "work_admission_audits", ["outcome"])
    op.create_index("ix_work_admission_audits_user_id", "work_admission_audits", ["user_id"])
    op.create_index(
        "ix_work_admission_audits_reference_id", "work_admission_audits", ["reference_id"]
    )


def downgrade() -> None:
    op.drop_table("work_admission_audits")
