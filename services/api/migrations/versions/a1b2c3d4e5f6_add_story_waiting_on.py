"""add stories.waiting_on

Revision ID: a1b2c3d4e5f6
Revises: d1e2f3a4b5c6
Create Date: 2026-09-02 01:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept literal rather than imported: a migration describes the schema at this
# revision, and must not change when the enum later gains a value.
_WAITING_ON_BY_STATUS: dict[str, str] = {
    "created": "none",
    "in_progress": "none",
    "reopened": "none",
    "pr_review": "ci",
    "deploying": "deploy",
    "testing": "qa",
    "waiting_human_review": "human_review",
    "waiting_user_secret": "user_secret",
    "completed": "none",
    "failed": "none",
    "archived": "none",
}


def upgrade() -> None:
    # server_default makes the column non-nullable for existing rows in one
    # statement; the backfill below then gives every row the value its current
    # status implies, so the column is truthful the moment it exists.
    op.add_column(
        "stories",
        sa.Column("waiting_on", sa.String(length=50), nullable=False, server_default="none"),
    )
    op.create_index(op.f("ix_stories_waiting_on"), "stories", ["waiting_on"], unique=False)

    stories = sa.table(
        "stories",
        sa.column("status", sa.String),
        sa.column("waiting_on", sa.String),
    )
    for status, waiting_on in _WAITING_ON_BY_STATUS.items():
        if waiting_on == "none":
            continue
        op.execute(stories.update().where(stories.c.status == status).values(waiting_on=waiting_on))


def downgrade() -> None:
    op.drop_index(op.f("ix_stories_waiting_on"), table_name="stories")
    op.drop_column("stories", "waiting_on")
