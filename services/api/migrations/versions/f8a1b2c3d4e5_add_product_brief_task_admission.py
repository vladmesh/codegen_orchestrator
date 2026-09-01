"""Add durable Product Brief task-dispatch admission.

Revision ID: f8a1b2c3d4e5
Revises: e3f4a5b6c7d8
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f8a1b2c3d4e5"
down_revision: str | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "product_briefs",
        sa.Column("coverage_admitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "dispatch_admitted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.create_index("ix_tasks_dispatch_admitted", "tasks", ["dispatch_admitted"])
    op.alter_column("tasks", "dispatch_admitted", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_tasks_dispatch_admitted", table_name="tasks")
    op.drop_column("tasks", "dispatch_admitted")
    op.drop_column("product_briefs", "coverage_admitted_at")
