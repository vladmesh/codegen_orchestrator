"""add failure_metadata to tasks

Revision ID: a2b3c4d5e6f7
Revises: 3fce954a4410
Create Date: 2026-03-10 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: str | None = "3fce954a4410"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("failure_metadata", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "failure_metadata")
