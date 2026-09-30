"""Add the per-project record of checks QA could not perform.

Revision ID: 8b2d4f6a0c3e
Revises: 7a1c3e5d9b2f
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "8b2d4f6a0c3e"
down_revision: str | None = "7a1c3e5d9b2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "verification_gaps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_id", sa.String(length=255), nullable=True),
        sa.Column("run_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "origin IN ('executor', 'not_applicable', 'withheld', 'package')",
            name="ck_verification_gaps_origin",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "run_id", "name", name="uq_verification_gaps_run_check"),
    )
    op.create_index(
        "ix_verification_gaps_project_created", "verification_gaps", ["project_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_verification_gaps_project_created", table_name="verification_gaps")
    op.drop_table("verification_gaps")
