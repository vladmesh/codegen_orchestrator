"""owner_id NOT NULL on projects

Revision ID: f1a2b3c4d5e6
Revises: be730dff04d5
Create Date: 2026-03-06 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "be730dff04d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("projects", "owner_id", nullable=False)


def downgrade() -> None:
    op.alter_column("projects", "owner_id", nullable=True)
