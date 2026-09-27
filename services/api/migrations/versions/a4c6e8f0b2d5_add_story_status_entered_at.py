"""add status_entered_at to stories

When a story landed on its current status, stamped by the one status writer
(`_land_on`). Nullable and not backfilled: a row written before this revision has
no honest value, and readers show it as unknown rather than guess one from
`updated_at`, which unrelated writes move.

Revision ID: a4c6e8f0b2d5
Revises: d3f5a7c9e1b4
Create Date: 2026-09-27 15:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a4c6e8f0b2d5"
down_revision: str | None = "d3f5a7c9e1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stories", sa.Column("status_entered_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("stories", "status_entered_at")
