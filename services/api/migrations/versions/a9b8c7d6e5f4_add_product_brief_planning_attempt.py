"""Add durable Product Brief architect planning-attempt fence.

Revision ID: a9b8c7d6e5f4
Revises: f8a1b2c3d4e5
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a9b8c7d6e5f4"
down_revision: str | None = "f8a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("product_briefs", sa.Column("planning_attempt_id", sa.String(length=128)))
    op.add_column(
        "product_briefs",
        sa.Column(
            "planning_attempt_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "product_briefs", sa.Column("planning_attempt_heartbeat_at", sa.DateTime(timezone=True))
    )
    op.alter_column("product_briefs", "planning_attempt_active", server_default=None)
    op.add_column("tasks", sa.Column("planning_attempt_id", sa.String(length=128)))
    op.create_index("ix_tasks_planning_attempt_id", "tasks", ["planning_attempt_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_planning_attempt_id", table_name="tasks")
    op.drop_column("tasks", "planning_attempt_id")
    op.drop_column("product_briefs", "planning_attempt_heartbeat_at")
    op.drop_column("product_briefs", "planning_attempt_active")
    op.drop_column("product_briefs", "planning_attempt_id")
