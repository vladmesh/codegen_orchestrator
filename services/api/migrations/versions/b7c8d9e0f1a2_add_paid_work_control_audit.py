"""Record immutable operator control changes for paid-work admission.

Revision ID: b7c8d9e0f1a2
Revises: a3d4e5f6a7b8
Create Date: 2026-08-26 23:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b7c8d9e0f1a2"
down_revision: str | None = "a3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "work_admission_audits", sa.Column("control_name", sa.String(length=64), nullable=True)
    )
    op.add_column("work_admission_audits", sa.Column("before_value", sa.JSON(), nullable=True))
    op.add_column("work_admission_audits", sa.Column("after_value", sa.JSON(), nullable=True))
    op.add_column("work_admission_audits", sa.Column("actor", sa.String(length=128), nullable=True))
    op.create_index(
        op.f("ix_work_admission_audits_control_name"),
        "work_admission_audits",
        ["control_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_work_admission_audits_control_name"), table_name="work_admission_audits")
    op.drop_column("work_admission_audits", "actor")
    op.drop_column("work_admission_audits", "after_value")
    op.drop_column("work_admission_audits", "before_value")
    op.drop_column("work_admission_audits", "control_name")
