"""add_blocked_by_task_id_to_tasks

Revision ID: 800ab7e1637d
Revises: c3d4e5f6a7b8
Create Date: 2026-03-08 02:22:23.216108

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "800ab7e1637d"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("blocked_by_task_id", sa.String(length=255), nullable=True))
    op.create_foreign_key(
        "fk_tasks_blocked_by_task_id", "tasks", "tasks", ["blocked_by_task_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_blocked_by_task_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "blocked_by_task_id")
