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
    """Add the column, then make it required.

    Rows that predate ownership have no run to name — the runs that made them
    were never recorded. They are stamped with their own project id so the
    column can be NOT NULL, which is what makes every project created from here
    on carry a run supplied by whoever started it.
    """
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=64), nullable=True))
    op.execute("UPDATE projects SET initiating_run_id = id::text WHERE initiating_run_id IS NULL")
    op.alter_column(TABLE, COLUMN, existing_type=sa.String(length=64), nullable=False)
    op.create_index(f"ix_{TABLE}_{COLUMN}", TABLE, [COLUMN])


def downgrade() -> None:
    op.drop_index(f"ix_{TABLE}_{COLUMN}", table_name=TABLE)
    op.drop_column(TABLE, COLUMN)
