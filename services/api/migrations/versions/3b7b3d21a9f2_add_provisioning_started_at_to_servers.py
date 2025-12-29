"""Add provisioning_started_at to servers

Revision ID: 3b7b3d21a9f2
Revises: f906c524c544
Create Date: 2025-12-26 19:10:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "3b7b3d21a9f2"
down_revision: str | None = "f906c524c544"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("provisioning_started_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "provisioning_started_at")
