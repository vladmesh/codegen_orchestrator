"""Create stories table and add Task.story_id

Revision ID: b265e9f78a39
Revises: a154d8c67e28
Create Date: 2026-03-08 03:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b265e9f78a39"
down_revision: str | None = "a154d8c67e28"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stories",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(255),
            sa.ForeignKey("projects.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "parent_story_id",
            sa.String(255),
            sa.ForeignKey("stories.id"),
            nullable=True,
            index=True,
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("acceptance_criteria", sa.Text(), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="created"),
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

    op.add_column(
        "tasks",
        sa.Column(
            "story_id",
            sa.String(255),
            sa.ForeignKey("stories.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_tasks_story_id", "tasks", ["story_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_story_id", "tasks")
    op.drop_column("tasks", "story_id")
    op.drop_table("stories")
