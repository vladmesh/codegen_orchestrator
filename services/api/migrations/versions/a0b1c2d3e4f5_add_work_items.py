"""Add work_items and work_item_events tables, link tasks to work_items

Revision ID: a0b1c2d3e4f5
Revises: f1a2b3c4d5e6
Create Date: 2026-03-07 18:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a0b1c2d3e4f5"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "work_items",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(255),
            sa.ForeignKey("projects.id"),
            nullable=True,
            index=True,
        ),
        sa.Column("type", sa.String(50), nullable=False, server_default="feature"),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="backlog", index=True),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0", index=True),
        sa.Column("acceptance_criteria", sa.Text, nullable=True),
        sa.Column("current_iteration", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_iterations", sa.Integer, nullable=False, server_default="3"),
        sa.Column("created_by", sa.String(50), nullable=False, server_default="system"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "work_item_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "work_item_id",
            sa.String(255),
            sa.ForeignKey("work_items.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("event_type", sa.String(50), nullable=False, server_default="note"),
        sa.Column("from_status", sa.String(50), nullable=True),
        sa.Column("to_status", sa.String(50), nullable=True),
        sa.Column("iteration", sa.Integer, nullable=True),
        sa.Column("details", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("actor", sa.String(50), nullable=False, server_default="system"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Add work_item_id and iteration to existing tasks table
    op.add_column(
        "tasks",
        sa.Column(
            "work_item_id",
            sa.String(255),
            sa.ForeignKey("work_items.id"),
            nullable=True,
            index=True,
        ),
    )
    op.add_column("tasks", sa.Column("iteration", sa.Integer, nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "iteration")
    op.drop_column("tasks", "work_item_id")
    op.drop_table("work_item_events")
    op.drop_table("work_items")
