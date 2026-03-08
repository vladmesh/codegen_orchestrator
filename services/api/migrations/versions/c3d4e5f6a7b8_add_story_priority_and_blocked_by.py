"""Add priority and blocked_by_story_id to stories

Revision ID: c3d4e5f6a7b8
Revises: b265e9f78a39
Create Date: 2026-03-08 04:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b265e9f78a39"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "stories",
        sa.Column(
            "blocked_by_story_id",
            sa.String(255),
            sa.ForeignKey("stories.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_stories_priority", "stories", ["priority"])
    op.create_index("ix_stories_blocked_by_story_id", "stories", ["blocked_by_story_id"])


def downgrade() -> None:
    op.drop_index("ix_stories_blocked_by_story_id", "stories")
    op.drop_index("ix_stories_priority", "stories")
    op.drop_column("stories", "blocked_by_story_id")
    op.drop_column("stories", "priority")
