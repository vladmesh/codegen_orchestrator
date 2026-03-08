"""Add story.type field, drop milestone_id from tasks, drop milestones table.

Revision ID: 3fce954a4410
Revises: f4e731766f8c
Create Date: 2026-03-08
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "3fce954a4410"
down_revision: str | None = "f4e731766f8c"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. Add type column to stories (default 'product')
    op.add_column(
        "stories", sa.Column("type", sa.String(50), nullable=False, server_default="product")
    )

    # 2. Drop milestone_id FK and column from tasks
    # FK/index names vary between fresh DB and legacy DB
    conn = op.get_bind()
    insp = inspect(conn)
    fks = insp.get_foreign_keys("tasks")
    milestone_fk = next(
        (fk["name"] for fk in fks if "milestone_id" in fk.get("constrained_columns", [])),
        None,
    )
    if milestone_fk:
        op.drop_constraint(milestone_fk, "tasks", type_="foreignkey")
    indexes = insp.get_indexes("tasks")
    milestone_idx = next(
        (idx["name"] for idx in indexes if "milestone_id" in idx.get("column_names", [])),
        None,
    )
    if milestone_idx:
        op.drop_index(milestone_idx, table_name="tasks")
    op.drop_column("tasks", "milestone_id")

    # 3. Drop milestones table
    op.drop_table("milestones")


def downgrade() -> None:
    # 1. Recreate milestones table
    op.create_table(
        "milestones",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(50), nullable=False, server_default="open"),
        sa.Column("parent_id", sa.String(255), sa.ForeignKey("milestones.id"), nullable=True),
        sa.Column("created_by", sa.String(50), nullable=False, server_default="system"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_milestones_project_id", "milestones", ["project_id"])

    # 2. Re-add milestone_id to tasks
    op.add_column(
        "tasks",
        sa.Column("milestone_id", sa.String(255), sa.ForeignKey("milestones.id"), nullable=True),
    )
    op.create_index("ix_tasks_milestone_id", "tasks", ["milestone_id"])

    # 3. Drop type from stories
    op.drop_column("stories", "type")
