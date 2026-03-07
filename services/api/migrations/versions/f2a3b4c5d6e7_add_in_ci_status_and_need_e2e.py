"""Add in_ci status and need_e2e field

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-03-08 00:30:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Rename in_review → in_ci in tasks table
    op.execute("UPDATE tasks SET status = 'in_ci' WHERE status = 'in_review'")

    # Rename in_review → in_ci in task_events (from_status and to_status)
    op.execute("UPDATE task_events SET from_status = 'in_ci' WHERE from_status = 'in_review'")
    op.execute("UPDATE task_events SET to_status = 'in_ci' WHERE to_status = 'in_review'")

    # Add need_e2e column
    op.add_column("tasks", sa.Column("need_e2e", sa.Boolean(), server_default="false"))


def downgrade() -> None:
    op.drop_column("tasks", "need_e2e")

    op.execute("UPDATE task_events SET to_status = 'in_review' WHERE to_status = 'in_ci'")
    op.execute("UPDATE task_events SET from_status = 'in_review' WHERE from_status = 'in_ci'")
    op.execute("UPDATE tasks SET status = 'in_review' WHERE status = 'in_ci'")
