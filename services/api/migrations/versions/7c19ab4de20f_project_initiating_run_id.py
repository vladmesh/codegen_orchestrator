"""Record the run that initiated a project's work

Revision ID: 7c19ab4de20f
Revises: e8f9a0b1c2d3
Create Date: 2026-08-13 15:10:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "7c19ab4de20f"
down_revision: str | None = "e8f9a0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "projects"
COLUMN = "initiating_run_id"


def upgrade() -> None:
    """Add the column. Rows that predate it keep NULL.

    A project created before this migration was created by a run nobody wrote
    down, and no value here can recover it. Backfilling one — the project id,
    a minted id, a constant — would put a value that is not a run into
    `com.codegen.run.id` on every worker such a project spawns from then on,
    and two unrelated later runs on that project would answer the same
    run-scoped label query. So the column is nullable and legacy rows stay
    empty: absence is representable, and it is refused at the point where a
    worker would be created (`require_initiating_run`) rather than papered
    over. Every project created from here on carries a run supplied by
    whoever started it, because `ProjectCreate.initiating_run_id` requires it.
    """
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=64), nullable=True))
    op.create_index(f"ix_{TABLE}_{COLUMN}", TABLE, [COLUMN])


def downgrade() -> None:
    op.drop_index(f"ix_{TABLE}_{COLUMN}", table_name=TABLE)
    op.drop_column(TABLE, COLUMN)
