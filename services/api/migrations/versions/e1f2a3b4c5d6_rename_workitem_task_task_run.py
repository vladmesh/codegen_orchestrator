"""Rename WorkItem→Task, Task→Run

Revision ID: e1f2a3b4c5d6
Revises: d9d613e270c2
Create Date: 2026-03-07 19:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d9d613e270c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Step 1: Rename execution-layer table tasks → runs (avoids collision)
    op.rename_table("tasks", "runs")

    # Step 2: Rename planning-layer table work_items → tasks
    op.rename_table("work_items", "tasks")

    # Step 3: Rename work_item_events → task_events
    op.rename_table("work_item_events", "task_events")

    # Step 4: Rename FK column runs.work_item_id → runs.task_id
    op.alter_column("runs", "work_item_id", new_column_name="task_id")

    # Step 5: Rename FK column task_events.work_item_id → task_events.task_id
    op.alter_column("task_events", "work_item_id", new_column_name="task_id")


def downgrade() -> None:
    # Reverse step 5
    op.alter_column("task_events", "task_id", new_column_name="work_item_id")

    # Reverse step 4
    op.alter_column("runs", "task_id", new_column_name="work_item_id")

    # Reverse step 3
    op.rename_table("task_events", "work_item_events")

    # Reverse step 2
    op.rename_table("tasks", "work_items")

    # Reverse step 1
    op.rename_table("runs", "tasks")
